"""
Stage 2: free email checks, per contact. `pending` -> `checked` or `skipped_invalid`.

Every contact of a pending company gets check_passed / check_reason / checked_at on ae_contacts.
Checks run in this order and the first failure is the reason:

    syntax -> free_mail -> role_address -> disposable -> suppressed -> no_mx

A company passes (`checked`) when at least one contact passes; otherwise it is `skipped_invalid`
and, as stage 4 only scans `checked` companies, never scanned.

MX: a real lookup with a timeout (settings_checks.MX_TIMEOUT_SECONDS), cached per domain in
ae_mx_cache. A lookup that times out or SERVFAILs is not evidence of anything, so it is NOT a
failure and NOT cached as an answer: the contact passes with check_reason 'mx_unverified', the
row in ae_mx_cache records has_mx NULL, and the next company at that domain asks again.
NXDOMAIN, an empty answer and a null MX (RFC 7505) are definite: no_mx.

No CRM access at all, and these status changes have no CRM mirror.
"""
import re
from collections import Counter
from functools import lru_cache

import db

from . import settings_checks as cfg
from . import status

_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_TLD = re.compile(r"^([A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})$")


class NoMx(Exception):
    """The domain definitely cannot receive mail (NXDOMAIN, no answer, null MX)."""


class MxUnavailable(Exception):
    """The lookup itself failed (timeout, SERVFAIL): no evidence either way."""


def email_domain(email):
    return email.rsplit("@", 1)[1].strip().lower()


def syntax_ok(email):
    """A plain address: sane local part, dotted domain with a letters-only TLD, within RFC length limits."""
    if not isinstance(email, str) or email != email.strip() or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    if not local or len(local) > 64 or len(email) > 254 or not _LOCAL.match(local):
        return False
    labels = domain.split(".")
    return (len(labels) >= 2 and all(_LABEL.match(l) for l in labels) and bool(_TLD.match(labels[-1])))


def is_free_mail(email):
    return email_domain(email) in cfg.FREE_MAIL_DOMAINS


def is_role_address(email):
    return email.split("@")[0].strip().lower() in cfg.ROLE_LOCAL_PARTS


@lru_cache(maxsize=1)
def disposable_domains():
    """The bundled blocklist, read once."""
    lines = cfg.DISPOSABLE_FILE.read_text().splitlines()
    return frozenset(l.strip().lower() for l in lines if l.strip() and not l.startswith("#"))


def is_disposable(email):
    return email_domain(email) in disposable_domains()


def dns_resolver(domain, timeout):
    """The real MX lookup. Returns MX hosts; raises NoMx or MxUnavailable."""
    import dns.exception
    import dns.resolver
    try:
        answer = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        hosts = [str(r.exchange).rstrip(".").lower() for r in answer]
        hosts = [h for h in hosts if h]                      # a null MX is a single "."
        if not hosts:
            raise NoMx("null MX")
        return hosts
    except dns.resolver.NXDOMAIN:
        raise NoMx("NXDOMAIN")
    except dns.resolver.NoAnswer:
        try:                                                 # RFC 5321: no MX means the A record receives mail
            dns.resolver.resolve(domain, "A", lifetime=timeout)
            return [domain]
        except dns.resolver.NXDOMAIN:
            raise NoMx("NXDOMAIN")
        except dns.resolver.NoAnswer:
            raise NoMx("no MX and no A record")
        except dns.exception.DNSException as exc:
            raise MxUnavailable(type(exc).__name__)
    except dns.exception.DNSException as exc:                # Timeout, LifetimeTimeout, NoNameservers, ...
        raise MxUnavailable(type(exc).__name__)


class _Mx:
    """MX answers for one run: the ae_mx_cache table first, then the resolver."""

    def __init__(self, cur, resolver, dry_run):
        self.cur, self.resolver, self.dry_run, self.memo = cur, resolver, dry_run, {}

    def has_mx(self, domain):
        """True / False, or None when the lookup was unavailable."""
        if domain in self.memo:
            return self.memo[domain]
        self.cur.execute("""SELECT has_mx FROM ae_mx_cache WHERE domain = %s AND has_mx IS NOT NULL
                            AND checked_at > NOW() - make_interval(days => %s)""",
                         (domain, cfg.MX_CACHE_DAYS))
        row = self.cur.fetchone()
        if row:
            self.memo[domain] = row["has_mx"]
            return row["has_mx"]
        try:
            hosts, detail, result = self.resolver(domain, cfg.MX_TIMEOUT_SECONDS), None, True
            detail = ", ".join(hosts[:3])
        except NoMx as exc:
            detail, result = str(exc), False
        except MxUnavailable as exc:
            detail, result = str(exc), None
        if not self.dry_run:
            self.cur.execute("""INSERT INTO ae_mx_cache (domain, has_mx, detail) VALUES (%s, %s, %s)
                                ON CONFLICT (domain) DO UPDATE
                                SET has_mx = EXCLUDED.has_mx, detail = EXCLUDED.detail, checked_at = NOW()""",
                             (domain, result, detail))
        if result is not None:                               # an unavailable lookup is asked again
            self.memo[domain] = result
        return result


def check_contact(cur, email, mx):
    """(passed, reason) for one address. reason is None on a clean pass, 'mx_unverified' on a flagged one."""
    email = (email or "").strip()
    if not syntax_ok(email):
        return False, "syntax"
    if is_free_mail(email) and not cfg.ALLOW_FREE_MAIL:
        return False, "free_mail"
    if is_role_address(email):
        return False, "role_address"
    if is_disposable(email):
        return False, "disposable"
    cur.execute("SELECT 1 FROM ae_suppression WHERE email = LOWER(%s)", (email,))
    if cur.fetchone():
        return False, "suppressed"
    has_mx = mx.has_mx(email_domain(email))
    if has_mx is False:
        return False, "no_mx"
    return True, ("mx_unverified" if has_mx is None else None)


def run(limit=None, dry_run=False, only_test=None, resolver=None):
    """
    Stage 2 over up to `limit` pending companies. only_test: None = all, True = the test cohort,
    False = production only. dry_run writes nothing (no contact results, no MX cache rows, no
    status changes) and returns what it would have done.
    """
    counts = {"claimed": 0, "checked": 0, "skipped_invalid": 0, "contacts_passed": 0,
              "contacts_failed": 0, "fail_reasons": {}, "warnings": {}}
    fails, warns = Counter(), Counter()
    with status.claim("pending", limit, is_test=only_test) as (cur, rows):
        mx = _Mx(cur, resolver or dns_resolver, dry_run)
        for row in rows:
            counts["claimed"] += 1
            cur.execute("""SELECT contact_id, email, check_passed FROM ae_contacts
                           WHERE company_id = %s ORDER BY id""", (row["company_id"],))
            contacts = cur.fetchall()
            company_fails, passed = Counter(), 0
            for ct in contacts:
                if ct["check_passed"] is None:
                    ok, reason = check_contact(cur, ct["email"], mx)
                    if not dry_run:
                        cur.execute("""UPDATE ae_contacts SET check_passed = %s, check_reason = %s,
                                       checked_at = NOW() WHERE contact_id = %s""",
                                    (ok, reason, ct["contact_id"]))
                    counts["contacts_passed" if ok else "contacts_failed"] += 1
                    if not ok:
                        fails[reason] += 1
                        company_fails[reason] += 1
                    elif reason:
                        warns[reason] += 1
                else:
                    ok = ct["check_passed"]
                passed += bool(ok)
            if passed:
                new, why = "checked", f"{passed} of {len(contacts)} contacts passed the free checks"
            elif not contacts:
                new, why = "skipped_invalid", "no contacts"
            else:
                new = "skipped_invalid"
                why = "no passing contact: " + ", ".join(
                    f"{n} {r}" for r, n in sorted(company_fails.items(), key=lambda kv: (-kv[1], kv[0])))
            counts[new] += 1
            if not dry_run:
                status.set_status(cur, row["company_id"], new, why)
    counts["fail_reasons"], counts["warnings"] = dict(fails), dict(warns)
    return counts
