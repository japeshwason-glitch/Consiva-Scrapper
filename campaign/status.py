"""
The company status model: every status, every allowed move, the one function
that changes a status, and row claiming.

    set_status(cur, company_id, new_status, reason)   -- the ONLY way a status changes
    claim(status, limit)                              -- rows locked FOR UPDATE SKIP LOCKED

A claim holds its rows locked until the `with` block ends, and set_status runs
on the claim's cursor, so a row is claimed, worked and moved in one transaction:
a crash rolls the whole thing back and the row is claimable again.
"""
from contextlib import contextmanager

import psycopg2.extras

import db

from . import crm_write, mirror

STATUSES = (
    "pending", "checked", "scanned", "gate_passed", "reported", "contact_ranked",
    "verified", "drafted", "written_to_crm", "sent", "expired", "skipped_invalid",
    "no_website", "scan_failed", "score_too_high", "no_gaps", "held_no_contact",
    "held_already_contacted", "suppressed",
    # Added when the pipeline took over ScrapeStatus / ReportStatus:
    "unscannable",     # the site answered but could not be read (HTTP error, bot wall, parked page)
    "legacy",          # processed by the retired report script; imported once, no stages
    "held_duplicate_domain",   # another company already holds this domain; reason = that company's id
)
INITIAL = ("pending", "legacy")      # the statuses a company may be inserted with

# status -> the statuses it may move to. The happy path is a straight line;
# every other status is an exit from one stage. Terminal statuses go nowhere.
TRANSITIONS = {
    "pending": frozenset({"checked", "skipped_invalid", "no_website", "held_already_contacted"}),
    # `checked` covers stage 3 (domain) as well as the scan: a checked company may still be
    # stopped for having no usable domain, or for sharing one with an earlier company.
    "checked": frozenset({"scanned", "scan_failed", "unscannable", "no_website", "held_duplicate_domain"}),
    "scanned": frozenset({"gate_passed", "score_too_high", "no_gaps"}),
    "gate_passed": frozenset({"reported"}),
    "reported": frozenset({"contact_ranked", "held_no_contact"}),
    "contact_ranked": frozenset({"verified", "suppressed"}),
    "verified": frozenset({"drafted", "suppressed"}),   # suppressed: the chosen contact was suppressed after verification
    "drafted": frozenset({"written_to_crm", "expired"}),
    "written_to_crm": frozenset({"sent", "expired"}),
    "sent": frozenset(),
    "expired": frozenset(),
    "skipped_invalid": frozenset(),
    "no_website": frozenset(),
    "scan_failed": frozenset(),
    "score_too_high": frozenset(),
    "no_gaps": frozenset(),
    "held_no_contact": frozenset(),
    "held_already_contacted": frozenset(),
    "suppressed": frozenset(),
    "unscannable": frozenset(),
    "held_duplicate_domain": frozenset(),
    # Only a test company may leave `legacy` (the test cohort re-opens legacy rows);
    # set_status() enforces that. For every other company legacy is final.
    "legacy": frozenset({"pending"}),
}
TERMINAL = tuple(s for s, nxt in TRANSITIONS.items() if not nxt)


class InvalidTransition(Exception):
    pass


class UnknownCompany(Exception):
    pass


def is_allowed(current, new):
    return new in TRANSITIONS.get(current, frozenset())


def set_status(cur, company_id, new_status, reason, crm=None, write_crm=True):
    """
    Moves one company to new_status, in the caller's transaction. The row is locked first
    so two workers cannot both move it. Raises InvalidTransition for a move the model does
    not allow, ValueError for a missing reason, UnknownCompany for an id we do not hold.
    Nothing is changed in any of those.

    The CRM mirror is part of the same step: when new_status has CRM values (mirror.CRM_VALUES)
    and the company is a production company, they are written to the CRM here. If that write
    fails, the status change is undone and the error propagates, so the stage fails and
    retries; ae_companies and the CRM do not drift apart. Test companies never write.

    `crm` carries the extra CRM columns that belong to this step and are only known at run time
    (the six consent columns and their timestamps, the report URL); they are written together with
    the static ones in mirror.CRM_VALUES, in one UPDATE. write_crm=False skips the CRM entirely (adopting a company
    whose report already exists: adopt.py). crm_write allow-lists and validates every
    one of them.
    """
    if not (reason or "").strip():
        raise ValueError(f"a reason is required to move company {company_id} to {new_status}")
    if new_status not in TRANSITIONS:
        raise InvalidTransition(f"{new_status!r} is not a status")
    cur.execute("SELECT status, is_test FROM ae_companies WHERE company_id = %s FOR UPDATE", (company_id,))
    row = cur.fetchone()
    if row is None:
        raise UnknownCompany(f"company {company_id} is not in ae_companies")
    current, is_test = row["status"], row["is_test"]
    if not is_allowed(current, new_status):
        raise InvalidTransition(
            f"company {company_id}: {current} -> {new_status} is not allowed "
            f"(from {current}: {sorted(TRANSITIONS.get(current, ())) or 'terminal'})")
    if current == "legacy" and not is_test:
        raise InvalidTransition(f"company {company_id} is a legacy company; it can only be re-opened "
                                f"as a test company (intake.promote_to_test)")
    crm_columns = None
    if not is_test and write_crm:
        merged = {**mirror.CRM_VALUES.get(new_status, {}), **(crm or {})}
        crm_columns = merged or None
    cur.execute("SAVEPOINT ae_set_status")
    cur.execute("""UPDATE ae_companies SET status = %s, status_reason = %s, updated_at = NOW()
                   WHERE company_id = %s""", (new_status, reason.strip(), company_id))
    if crm_columns:
        try:
            crm_write.update_company(company_id, **crm_columns)
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT ae_set_status")      # even if the caller swallows the error
            raise
    cur.execute("RELEASE SAVEPOINT ae_set_status")


_SCOPE = None        # the company ids this run is limited to, or None for no limit


@contextmanager
def only_companies(ids):
    """
    Limits every stage's company selection to these CRM ids for the duration of the block (prepare --ids and
    --adopt-ids). Nothing outside the set is read, claimed, scanned, written or drafted by any stage.
    """
    global _SCOPE
    previous, _SCOPE = _SCOPE, (set(ids) if ids else None)
    try:
        yield
    finally:
        _SCOPE = previous


def scope_sql():
    """(" AND company_id = ANY(%s)", [ids]) while a scope is active, else ("", [])."""
    return (" AND company_id = ANY(%s)", [sorted(_SCOPE)]) if _SCOPE else ("", [])


@contextmanager
def step(cur):
    """
    One company's work inside a larger transaction (a claim handling many rows). If the block
    raises, everything it did to this connection since the block began is undone and the error
    propagates, so the caller can count it and carry on with the next company.
    """
    cur.execute("SAVEPOINT ae_step")
    try:
        yield
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT ae_step")
        raise
    else:
        cur.execute("RELEASE SAVEPOINT ae_step")


@contextmanager
def claim(status, limit, is_test=None, where=None, params=()):
    """
    Locks up to `limit` companies at `status` (oldest first), skipping rows
    another worker holds, and yields (cursor, rows). Commits when the block
    exits cleanly, rolls back on an exception. `where` adds an extra SQL condition on
    ae_companies (with its `params`), for example "domain_source IS NOT NULL".
    """
    if status not in TRANSITIONS:
        raise ValueError(f"{status!r} is not a status")
    extra_where, extra_params = where, list(params)
    where, params = "status = %s", [status]
    if is_test is not None:
        where += " AND is_test = %s"
        params.append(is_test)
    if extra_where:
        where += f" AND ({extra_where})"
        params.extend(extra_params)
    scope, scope_params = scope_sql()
    where += scope
    params.extend(scope_params)
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"""SELECT * FROM ae_companies WHERE {where}
                            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", (*params, limit))
            yield cur, cur.fetchall()
