"""
Stage 8: NeverBounce verification and marking. contact_ranked -> verified | suppressed.

    run(limit=None, dry_run=False, only_test=None, client=None) -> counts

For each contact_ranked company the gate must be open before any call is made:
  * the contact passed stage 2 (ae_contacts.check_passed) and was ranked by stage 7;
  * the report exists (report_url) and is younger than REPORT_FRESH_DAYS (report_date);
  * the address is not in ae_suppression (such a contact is DO_NOT_EMAIL, no call);
  * no cached verdict younger than CACHE_DAYS (if there is, it is used and nothing is spent);
  * today's credit cap (IST calendar day) is not used up, and in live mode the account balance
    covers the run.
The top-ranked passing contact is verified; invalid / disposable -> DO_NOT_EMAIL, suppressed, then
the next-ranked contact, up to FALLBACKS more; valid -> OK; catchall / unknown -> RISKY, accepted
without checking anyone else. A company with an OK contact is `verified`; with only a RISKY one,
`verified` with risky = TRUE; with every checked contact DO_NOT_EMAIL, `suppressed`.

Where a verdict came from matters: every verdict carries a source, 'live' (the real API) or 'mock',
stored on ae_contacts.nb_source and ae_verification_cache.source (NULL = unknown), and a verdict served
from the cache keeps the source it was written with. A PRODUCTION company (is_test = FALSE) may reach
`verified` only on a LIVE verdict: a run that could touch production with a mock client refuses to run
(stopped = 'mock_in_production'); a mock- or unknown-source cached verdict is ignored for production
and overwritten by the live verdict; and a last per-company check refuses to verify a production company
whose chosen verdict is not live. The test cohort may use any verdict. Mock verdicts never reach
ae_suppression, which production reads. With no API key, live mode stops cleanly
(stopped = 'missing_api_key'): no exception, nothing changed.

An API outage is never a verdict: a transient error stops the stage and leaves the company
contact_ranked, nothing marked, nothing suppressed, the reserved credit given back.

Facts that cost money or are true whatever happens next (the credit ledger, the verdict cache, the
suppression list) are committed at once on their own connection; the company and contact rows move in
the claim's transaction. No CRM writes happen here.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import db

from . import neverbounce, settings_verify, status

IST = timezone(timedelta(hours=5, minutes=30))           # India has no daylight saving

DECISION = {"valid": "OK", "catchall": "RISKY", "unknown": "RISKY",
            "invalid": "DO_NOT_EMAIL", "disposable": "DO_NOT_EMAIL"}
NB_DEAD = ("invalid", "disposable")                         # verdicts that mean "do not email"
SUPPRESSED_VERDICT = "in_suppression_list"                  # nb_verdict for a contact we never had to ask about


def _may_verify(co, source):
    """The final rule: a production company is verified only on a live verdict; a test company on any."""
    return bool(co["is_test"]) or source == "live"


def _trusted(co, source):
    """The same rule, applied up front to decide whether a stored or cached verdict may be used at all."""
    return bool(co["is_test"]) or source == "live"


def today_ist():
    return datetime.now(IST).date()


def _new_counts(mode, dry_run):
    return {"mode": mode, "dry_run": dry_run, "companies_seen": 0, "verified": 0, "verified_risky": 0,
            "suppressed": 0, "incomplete": 0, "blocked": {}, "cache_hits": 0, "api_calls": 0,
            "credits_used": 0, "credits_a_live_run_would_use": 0, "credits_worst_case": None,
            "stopped": None}


# --- reading ----------------------------------------------------------------------------------
def _gate_company(co):
    """The reason the company's report blocks stage 8, or None."""
    if not co["report_url"]:
        return "no_report", "stage 8 blocked: the report has not been generated"
    date = co["report_date"]
    if date is None or date < datetime.now(timezone.utc) - timedelta(days=settings_verify.REPORT_FRESH_DAYS):
        return "report_stale", (f"stage 8 blocked: the report is older than "
                                f"{settings_verify.REPORT_FRESH_DAYS} days or undated")
    return None


def _candidates(cur, company_id):
    """Ranked contacts that passed stage 2, best first."""
    cur.execute("""SELECT * FROM ae_contacts WHERE company_id = %s AND rank IS NOT NULL
                   AND check_passed IS TRUE ORDER BY rank, id""", (company_id,))
    return [dict(r) for r in cur.fetchall()]


def _email(ct):
    return (ct["email"] or "").strip().lower()


def _suppressed_set(cur, emails):
    if not emails:
        return set()
    cur.execute("SELECT email FROM ae_suppression WHERE email = ANY(%s)", (list(emails),))
    return {r["email"] for r in cur.fetchall()}


def _cached(cur, emails, co=None):
    """
    {email: (verdict, source)} for verdicts younger than CACHE_DAYS. With `co`, rows whose source this
    company may not rely on (anything but live, for a production company) are left out, so they are
    neither served nor counted as "no call needed".
    """
    if not emails:
        return {}
    cur.execute("""SELECT email, verdict, source FROM ae_verification_cache
                   WHERE email = ANY(%s) AND checked_at > NOW() - make_interval(days => %s)""",
                (list(emails), settings_verify.CACHE_DAYS))
    rows = {r["email"]: (r["verdict"], r["source"]) for r in cur.fetchall()}
    return {e: v for e, v in rows.items() if co is None or _trusted(co, v[1])}


def _fresh_decision(ct):
    """The contact's own earlier verdict (from a run cut short), if it is still inside the cache window."""
    if ct["nb_decision"] == "NOT_CHECKED" or ct["nb_checked_at"] is None:
        return None
    if ct["nb_checked_at"] < datetime.now(timezone.utc) - timedelta(days=settings_verify.CACHE_DAYS):
        return None
    return ct["nb_decision"]


def _usable_decision(co, ct):
    """
    _fresh_decision, minus a good verdict (OK / RISKY) this company may not rely on. A do-not-email
    decision is always honoured: it can only ever stop an email, never allow one.
    """
    decision = _fresh_decision(ct)
    if decision in ("OK", "RISKY") and not _trusted(co, ct["nb_source"]):
        return None
    return decision


def _evaluate(cur, co):
    """
    What stage 8 would do for one company, without doing it:
      {"blocked": (code, reason) | None, "contacts": [...], "best": credits, "worst": credits}
    best = credits if everything goes well (one check unless a cache hit settles it);
    worst = every uncached candidate checked, up to the fallback budget.
    """
    blocked = _gate_company(co)
    if blocked:
        return {"blocked": blocked, "contacts": [], "best": 0, "worst": 0}
    contacts = _candidates(cur, co["company_id"])
    if not contacts:
        return {"blocked": ("no_eligible_contact",
                            "stage 8 blocked: no ranked contact passed the stage 2 checks"),
                "contacts": [], "best": 0, "worst": 0}
    emails = [_email(c) for c in contacts]
    supp, cache = _suppressed_set(cur, emails), _cached(cur, emails, co)
    best, settled, worst = 0, False, 0
    for ct in contacts:
        e, dec = _email(ct), _usable_decision(co, ct)
        if dec == "DO_NOT_EMAIL" or e in supp:
            continue
        needs_call = dec is None and e not in cache
        if needs_call:
            worst += 1
        if not settled:
            settled = True
            best += 1 if needs_call else 0
    return {"blocked": None, "contacts": contacts, "best": best,
            "worst": min(worst, 1 + settings_verify.FALLBACKS)}


# --- ledger, cache, suppression: committed immediately, on their own connection ---------------------
def _used_today(day):
    with db.get_cursor() as cur:
        cur.execute("SELECT credits_used FROM ae_neverbounce_usage WHERE day = %s", (day,))
        row = cur.fetchone()
    return row["credits_used"] if row else 0


def _reserve_credit(day, cap):
    """Atomically takes one credit from today's cap. False if the cap is used up; never goes over."""
    if cap <= 0:
        return False
    with db.get_cursor() as cur:
        cur.execute("""INSERT INTO ae_neverbounce_usage (day, credits_used) VALUES (%s, 1)
                       ON CONFLICT (day) DO UPDATE SET credits_used = ae_neverbounce_usage.credits_used + 1
                       WHERE ae_neverbounce_usage.credits_used < %s RETURNING credits_used""", (day, cap))
        return cur.fetchone() is not None


def _release_credit(day):
    with db.get_cursor() as cur:
        cur.execute("""UPDATE ae_neverbounce_usage SET credits_used = GREATEST(credits_used - 1, 0)
                       WHERE day = %s""", (day,))


def _cache_verdict(email, verdict, source):
    with db.get_cursor() as cur:
        cur.execute("""INSERT INTO ae_verification_cache (email, verdict, checked_at, source) VALUES (%s, %s, NOW(), %s)
                       ON CONFLICT (email) DO UPDATE SET verdict = EXCLUDED.verdict, checked_at = NOW(),
                                                         source = EXCLUDED.source""",
                    (email, verdict, source))


def _suppress(email, reason="invalid", source="neverbounce"):
    with db.get_cursor() as cur:
        cur.execute("""INSERT INTO ae_suppression (email, reason, source) VALUES (%s, %s, %s)
                       ON CONFLICT (email) DO NOTHING""", (email, reason, source))


# --- one company ------------------------------------------------------------------------------------
class _Run:
    """State shared across the companies of one run."""

    def __init__(self, client, live, day, balance):
        self.client, self.live, self.day, self.balance = client, live, day, balance
        self.source = "live" if live else "mock"
        self.stopped = None
        self.prefetched = set()      # addresses asked in parallel by _prefetch: their verdicts are in the cache
        self.failed = set()          # addresses whose parallel call timed out or failed: left for the next run


def _mark(cur, ct, verdict, decision, source):
    cur.execute("""UPDATE ae_contacts SET nb_verdict = %s, nb_decision = %s, nb_checked_at = NOW(), nb_source = %s
                   WHERE contact_id = %s""", (verdict, decision, source, ct["contact_id"]))


def _ask(run, email, counts):
    """
    One verdict for an address from the API (or the mock). Returns the verdict, or None after setting
    run.stopped when the call cannot be made or did not answer.
    """
    if email in run.failed:                      # its parallel call failed: this company waits, the stage goes on
        return None
    if run.live:
        if run.balance is not None and run.balance < 1:
            run.stopped = "low_balance"
            return None
        if not _reserve_credit(run.day, settings_verify.DAILY_CREDIT_CAP):
            run.stopped = "daily_cap"
            return None
    try:
        verdict = run.client.check(email)
    except neverbounce.TransientError:
        if run.live:
            _release_credit(run.day)
        run.stopped = "transient_error"
        return None
    except neverbounce.NeverBounceError:
        if run.live:
            _release_credit(run.day)
        run.stopped = "api_error"
        return None
    counts["api_calls"] += 1
    counts["credits_a_live_run_would_use"] += 1
    if run.live:
        counts["credits_used"] += 1
        if run.balance is not None:
            run.balance -= 1
    _cache_verdict(email, verdict, run.source)
    return verdict


def _verify_company(cur, co, contacts, run, counts):
    """
    Walks the ranked contacts. Returns "verified", "verified_risky", "suppressed", "incomplete" or
    "blocked_non_live" (a production company whose verdict is not live: left exactly as it was).
    """
    if not _may_verify(co, run.source):
        return "blocked_non_live"            # a mock client must never be asked about a production company
    max_checks = 1 + settings_verify.FALLBACKS
    examined, chosen, risky, source = 0, None, False, None
    for ct in contacts:
        if examined >= max_checks:
            break
        email = _email(ct)
        decision = _usable_decision(co, ct)
        if decision in ("OK", "RISKY"):
            chosen, risky, source = ct, decision == "RISKY", ct["nb_source"]
            break
        if decision == "DO_NOT_EMAIL":                      # settled by an earlier, interrupted run
            examined += ct["nb_verdict"] in NB_DEAD
            continue
        if _suppressed_set(cur, [email]):
            _mark(cur, ct, SUPPRESSED_VERDICT, "DO_NOT_EMAIL", None)
            continue                                        # on the list: never asked about, costs no check
        hit = _cached(cur, [email], co).get(email)
        if hit:
            verdict, source = hit                           # served with the source it was written with
            if email not in run.prefetched:                 # a prefetched verdict is a call we paid for, not a hit
                counts["cache_hits"] += 1
        else:
            verdict = _ask(run, email, counts)
            if verdict is None:
                return "incomplete"
            source = run.source
        decision = DECISION[verdict]
        _mark(cur, ct, verdict, decision, source)
        if decision == "DO_NOT_EMAIL":
            if source == "live":                            # a fake verdict must not suppress a real address
                _suppress(email)
            examined += 1
            continue
        chosen, risky = ct, decision == "RISKY"
        break
    if chosen:
        if not _may_verify(co, source):                     # the last line of defence, per company
            return "blocked_non_live"
        cur.execute("UPDATE ae_companies SET chosen_contact_id = %s, risky = %s WHERE company_id = %s",
                    (chosen["contact_id"], risky, co["company_id"]))
        status.set_status(cur, co["company_id"], "verified",
                          f"NeverBounce ({source or 'unknown'}): contact {chosen['contact_id']} is "
                          f"{'RISKY' if risky else 'OK'}")
        return "verified_risky" if risky else "verified"
    status.set_status(cur, co["company_id"], "suppressed",
                      "NeverBounce: every checked contact is DO_NOT_EMAIL")
    return "suppressed"


def _first_call(cur, co, contacts):
    """The address _verify_company would ask NeverBounce about first for this company, or None if it needs no call."""
    for ct in contacts[: 1 + settings_verify.FALLBACKS]:
        email = _email(ct)
        decision = _usable_decision(co, ct)
        if decision in ("OK", "RISKY"):
            return None
        if decision == "DO_NOT_EMAIL" or _suppressed_set(cur, [email]):
            continue
        if _cached(cur, [email], co).get(email):
            return None
        return email
    return None


def _prefetch(cur, companies, plans, run, counts):
    """
    Asks NeverBounce about each company's first uncached address in parallel (settings_verify.CONCURRENCY at a
    time), before the sequential pass, which then finds the verdicts in the cache. Only the HTTP calls run in
    threads: credits are reserved here, one by one, and every verdict is cached and counted here, so the cap
    and the balance hold exactly as in the sequential path. A call that fails gives its credit back and the
    address is left to the next run (run.failed); if every call fails, the stage stops as it always did.
    """
    todo = []
    for co in companies:
        plan = plans[co["company_id"]]
        if plan["blocked"] or not _may_verify(co, run.source):
            continue
        email = _first_call(cur, co, plan["contacts"])
        if email and email not in todo:
            todo.append(email)
    reserved = []
    for email in todo:
        if run.balance is not None and run.balance - len(reserved) < 1:
            break                                    # the sequential pass reports low_balance
        if not _reserve_credit(run.day, settings_verify.DAILY_CREDIT_CAP):
            break                                    # ... and daily_cap
        reserved.append(email)
    if not reserved:
        return

    def call(email):
        try:
            return email, run.client.check(email), None
        except (neverbounce.TransientError, neverbounce.NeverBounceError) as exc:
            return email, None, exc

    answered, last_error = 0, None
    with ThreadPoolExecutor(max_workers=settings_verify.CONCURRENCY) as pool:
        for email, verdict, exc in pool.map(call, reserved):
            if exc is not None:
                _release_credit(run.day)
                run.failed.add(email)
                last_error = exc
                continue
            answered += 1
            counts["api_calls"] += 1
            counts["credits_a_live_run_would_use"] += 1
            counts["credits_used"] += 1
            if run.balance is not None:
                run.balance -= 1
            _cache_verdict(email, verdict, run.source)
            run.prefetched.add(email)
    counts["prefetch"] = {"asked": len(reserved), "answered": answered, "failed": len(run.failed)}
    if not answered:
        run.stopped = "api_error" if isinstance(last_error, neverbounce.NeverBounceError) else "transient_error"


def _note(cur, company_id, reason):
    """Records why a company was left alone, without touching its status."""
    cur.execute("UPDATE ae_companies SET status_reason = %s, updated_at = NOW() WHERE company_id = %s",
                (reason, company_id))


# --- the stage --------------------------------------------------------------------------------------
def _dry_run(limit, only_test, counts):
    with db.get_cursor() as cur:
        where, params = "status = 'contact_ranked'", []
        if only_test is not None:
            where += " AND is_test = %s"
            params.append(only_test)
        scope, scope_params = status.scope_sql()
        where += scope
        params.extend(scope_params)
        cur.execute(f"SELECT * FROM ae_companies WHERE {where} ORDER BY id LIMIT %s",
                    (*params, limit or settings_verify.DEFAULT_BATCH))
        companies = [dict(r) for r in cur.fetchall()]
        counts["companies_seen"] = len(companies)
        counts["credits_worst_case"] = 0
        for co in companies:
            plan = _evaluate(cur, co)
            if plan["blocked"]:
                counts["blocked"][plan["blocked"][0]] = counts["blocked"].get(plan["blocked"][0], 0) + 1
                continue
            counts["credits_a_live_run_would_use"] += plan["best"]
            counts["credits_worst_case"] += plan["worst"]
    return counts


MOCK_REFUSED = ("NeverBounce is using mock verdicts, and mock verdicts may only be used on the test cohort. "
                "This run's scope includes production companies, so nothing was done. Run it for the test "
                "cohort only, or set AE_NB_MODE=live with NEVERBOUNCE_API_KEY.")
KEY_MISSING = ("live mode needs NEVERBOUNCE_API_KEY in the environment and it is not set, so nothing was done. "
               "Set the key, or use AE_NB_MODE=mock for the test cohort.")


def _stop(counts, code, message):
    counts["stopped"], counts["error"] = code, message
    return counts


def run(limit=None, dry_run=False, only_test=None, client=None):
    mode = settings_verify.MODE
    counts = _new_counts(mode, dry_run)
    if mode == "off":
        return counts
    if dry_run:
        return _dry_run(limit, only_test, counts)           # a dry run asks nobody anything: no client, no key
    if client is None:
        if mode == "live" and not settings_verify.api_key():
            return _stop(counts, "missing_api_key", KEY_MISSING)
        client = neverbounce.make_client(mode)
    live, day = bool(client.live), today_ist()
    if not live and only_test is not True:                  # mock + any chance of production = refuse, entirely
        return _stop(counts, "mock_in_production", MOCK_REFUSED)

    with status.claim("contact_ranked", limit or settings_verify.DEFAULT_BATCH, is_test=only_test) as (cur, companies):
        counts["companies_seen"] = len(companies)
        plans = {co["company_id"]: _evaluate(cur, co) for co in companies}
        needed = sum(p["best"] for p in plans.values() if not p["blocked"])
        run_state = _Run(client, live, day, balance=None)

        if live and needed > 0:
            remaining_cap = settings_verify.DAILY_CREDIT_CAP - _used_today(day)
            if remaining_cap <= 0:
                run_state.stopped = "daily_cap"
            else:
                try:
                    run_state.balance = client.credits_remaining()
                except neverbounce.TransientError:
                    run_state.stopped = "transient_error"
                except neverbounce.NeverBounceError:
                    run_state.stopped = "api_error"
                else:
                    if run_state.balance is not None and run_state.balance < min(needed, remaining_cap):
                        run_state.stopped = "low_balance"

        if live and not run_state.stopped and settings_verify.CONCURRENCY > 1 and needed > 1:
            _prefetch(cur, companies, plans, run_state, counts)

        for co in companies:
            plan = plans[co["company_id"]]
            if plan["blocked"]:
                code, reason = plan["blocked"]
                counts["blocked"][code] = counts["blocked"].get(code, 0) + 1
                _note(cur, co["company_id"], reason)
                continue
            if run_state.stopped:
                continue                                   # stopped: later companies are left exactly as they were
            outcome = _verify_company(cur, co, plan["contacts"], run_state, counts)
            if outcome == "incomplete":
                counts["incomplete"] += 1
                _note(cur, co["company_id"], f"stage 8 stopped early ({run_state.stopped or 'address check failed'}); will retry")
                continue
            if outcome == "blocked_non_live":
                counts["blocked"]["non_live_verdict"] = counts["blocked"].get("non_live_verdict", 0) + 1
                _note(cur, co["company_id"], "stage 8 blocked: a production company needs a live verdict")
                continue
            counts[outcome] += 1
        counts["stopped"] = run_state.stopped
    return counts
