"""
Stage 3: the company's domain. Runs on `checked` companies whose domain_source is still NULL and
leaves a proceeding company in `checked` with domain and domain_source set; the scan stage picks
`checked` rows with a domain_source.

  * Website present (ae_companies.domain, cleaned at intake)        -> domain_source 'website'
  * Website empty: the domain of the contacts that passed stage 2 and use a business address,
    when they all share one or one covers at least DOMAIN_MAJORITY_SHARE of them
                                                                      -> domain_source 'email'
  * otherwise                                                          -> `no_website`
  * another company already holds the domain                          -> `held_duplicate_domain`,
    reason 'duplicate of company <id>'

A derived domain lives only in ae_companies. The CRM's Website column is never written (this
module has no CRM access). Duplicates: among non-test companies the lowest company_id proceeds;
a company that already holds the domain (domain_source set), and every legacy company, keeps it.
Test-cohort companies skip the rule (they are re-opened legacy rows, so their own domain is
usually "held" by their former self) and never hold a domain against production.
"""
from collections import Counter
from contextlib import contextmanager

import psycopg2.extras

import db

from . import checks, status
from . import settings_checks as cfg


@contextmanager
def _claim_unresolved(limit, only_test):
    """status.claim() for `checked` rows that still need a domain, lowest company_id first."""
    where, params = "status = 'checked' AND domain_source IS NULL", []
    if only_test is not None:
        where += " AND is_test = %s"
        params.append(only_test)
    scope, scope_params = status.scope_sql()
    where += scope
    params.extend(scope_params)
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""SELECT * FROM ae_companies WHERE {where}
                            ORDER BY company_id LIMIT %s FOR UPDATE SKIP LOCKED""", (*params, limit))
            yield cur, cur.fetchall()


def email_domain_for(cur, company_id):
    """(domain | None, why) from the passing business contacts of this company."""
    cur.execute("SELECT email FROM ae_contacts WHERE company_id = %s AND check_passed ORDER BY id", (company_id,))
    emails = [r["email"].strip() for r in cur.fetchall() if r["email"] and not checks.is_free_mail(r["email"].strip())]
    if not emails:
        return None, "no passing business contact to take a domain from"
    top, n = Counter(checks.email_domain(e) for e in emails).most_common(1)[0]
    if n / len(emails) >= cfg.DOMAIN_MAJORITY_SHARE:
        return top, None
    return None, f"contact email domains disagree ({n} of {len(emails)} at the most common one)"


def holder_of(cur, company_id, domain):
    """company_id of the non-test company that holds `domain` ahead of this one, or None."""
    cur.execute("""SELECT company_id FROM ae_companies
                   WHERE LOWER(domain) = %s AND company_id <> %s AND NOT is_test
                     AND status <> 'held_duplicate_domain'
                     AND (status = 'legacy' OR domain_source IS NOT NULL
                          OR (company_id < %s AND status IN ('pending', 'checked')))
                   ORDER BY company_id LIMIT 1""", (domain.lower(), company_id, company_id))
    row = cur.fetchone()
    return row["company_id"] if row else None


def run(limit=None, dry_run=False, only_test=None):
    """Stage 3 over up to `limit` checked companies. dry_run writes nothing and counts what would happen."""
    counts = {"claimed": 0, "website": 0, "email": 0, "no_website": 0, "held_duplicate_domain": 0}
    with _claim_unresolved(limit, only_test) as (cur, rows):
        for row in rows:
            counts["claimed"] += 1
            cid, domain, source = row["company_id"], row["domain"], "website"
            if not domain:
                source = "email"
                domain, why = email_domain_for(cur, cid)
                if not domain:
                    counts["no_website"] += 1
                    if not dry_run:
                        status.set_status(cur, cid, "no_website", f"no Website, and {why}")
                    continue
            if not row["is_test"]:
                other = holder_of(cur, cid, domain)
                if other is not None:
                    counts["held_duplicate_domain"] += 1
                    if not dry_run:
                        status.set_status(cur, cid, "held_duplicate_domain", f"duplicate of company {other}")
                    continue
            counts[source] += 1
            if not dry_run:
                cur.execute("UPDATE ae_companies SET domain = %s, domain_source = %s, updated_at = NOW() "
                            "WHERE company_id = %s", (domain, source, cid))
    return counts
