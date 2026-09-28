"""
Shared DB helper for the lead-gen pipeline.

Every stage script does the same three things:
    1. get_pending(status, limit) -- fetch the next batch waiting on this stage
    2. process it
    3. advance_status(cin, ...) on success, or mark_failed(cin, ...) on failure

This module is the only thing that touches psycopg2 directly -- stage scripts
never write raw SQL, so the schema can change in one place.

Connections come from a module-level pool rather than being opened per call:
several stage scripts run close together via cron, and a pool keeps the
connection setup cost off the per-row path.
"""

import atexit
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool

import config

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

MAX_RETRIES = 3  # after this many failures on a stage, a row moves to a *_failed status

# LLPs are not sent to the CRM (decided Sep 24): MCA carries no paid-up capital
# for them, so stage 05 cannot size them -- every LLP exported to that date was
# business_scale 'Unknown' -- and Apollo rarely has a usable person. MCA gives
# an LLP an LLPIN ("AAA-1234") in place of a CIN, which catches the ones whose
# name does not end in "LLP" too.
LLP_SQL = "cin ~ '^[A-Z]{3}-[0-9]{4}$'"

MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 10

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    """
    Lazily builds the connection pool on first use. Deferred rather than built
    at import time so that importing db.py (for MAX_RETRIES, say) doesn't
    require a reachable database.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not config.DATABASE_URL:
                    raise RuntimeError(
                        "DATABASE_URL is not set -- export it or put it in .env "
                        "(see .env.example)."
                    )
                _pool = psycopg2.pool.SimpleConnectionPool(
                    MIN_POOL_CONNECTIONS,
                    MAX_POOL_CONNECTIONS,
                    config.DATABASE_URL,
                )
    return _pool


def close_pool():
    """Closes every pooled connection. Registered atexit; safe to call twice."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


atexit.register(close_pool)


def init_db():
    """Creates the table and indexes if they don't exist yet. Safe to call every run."""
    with get_cursor() as cur:
        # psycopg2 has no executescript(); execute() runs a multi-statement
        # string directly as long as it carries no bound parameters.
        cur.execute(SCHEMA_PATH.read_text())


@contextmanager
def get_connection():
    """
    Checks a connection out of the pool, commits on clean exit, rolls back on
    exception, and always returns it to the pool afterward.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(cursor_factory=psycopg2.extras.RealDictCursor):
    """Convenience wrapper: a cursor on a pooled connection, same commit semantics."""
    with get_connection() as conn:
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur


_ALLOWED_ORDERS = {"random()", "updated_at ASC", "updated_at DESC"}


def get_pending(status, limit=100, extra_where=None, extra_params=(), order=None):
    """
    Fetch up to `limit` rows sitting at the given status, oldest-updated first
    (so a row that failed and got requeued doesn't get stuck behind new ones
    forever). `extra_where` lets a stage add its own filter, e.g. the
    consent-gap check before Apify runs: extra_where="has_cmp = false"

    has_cmp is a native BOOLEAN -- filter on true/false, never 0/1.

    `order` overrides the ordering. Pass "random()" to draw a spread across the
    whole corpus instead of working through it in ingest order -- the default
    walks state by state, because a bulk insert gives every row the same
    updated_at and ties break on physical order.
    """
    query = "SELECT * FROM companies WHERE status = %s"
    params = [status]
    if extra_where:
        # psycopg2 reads % as the start of a placeholder, so a literal one in
        # a LIKE pattern has to be doubled or the driver miscounts parameters
        # and raises IndexError. Callers write natural SQL -- "LIKE 'Foo%'" --
        # and the doubling happens here, once, rather than in each of them.
        # Skipped when the caller passes its own parameters, since then the %s
        # markers are deliberate and must survive.
        if not extra_params:
            extra_where = extra_where.replace("%", "%%")
        query += f" AND {extra_where}"
        params.extend(extra_params)
    if order:
        # Interpolated, so it is restricted to a fixed set rather than trusted.
        if order not in _ALLOWED_ORDERS:
            raise ValueError(f"order must be one of {sorted(_ALLOWED_ORDERS)}, got {order!r}")
        query += f" ORDER BY {order} LIMIT %s"
    else:
        query += " ORDER BY updated_at ASC LIMIT %s"
    params.append(limit)

    with get_cursor() as cur:
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]


def advance_status(cin, new_status, **fields):
    """
    Moves a company to the next status and writes any fields the stage
    produced, in one transaction. Resets retry_count -- a successful stage
    run means whatever failed before no longer matters.

    Caller-supplied fields override the retry_count/last_error defaults rather
    than being appended alongside them: mark_not_a_lead() passes its own
    last_error, and Postgres rejects a column assigned twice in one UPDATE.

    Usage: advance_status(cin, "resolved", resolved_domain="example.com",
                           tld_used="com", resolve_confidence=92.5)
    """
    fields = _serialize_json_fields(fields)
    defaults = {"retry_count": 0, "last_error": None}
    defaults.update(fields)  # caller's values win, no duplicate keys possible

    set_clauses = ", ".join(f"{k} = %s" for k in defaults)
    query = f"""
        UPDATE companies
        SET status = %s, {set_clauses}, updated_at = NOW()
        WHERE cin = %s
    """
    params = [new_status, *defaults.values(), cin]

    with get_cursor() as cur:
        cur.execute(query, params)


def mark_failed(cin, stage_status_prefix, error_msg):
    """
    Increments retry_count. If still under MAX_RETRIES, the row stays at its
    CURRENT status so the same stage picks it up again next run (a transient
    network blip shouldn't permanently drop a company). Past MAX_RETRIES, it
    moves to a terminal f"{stage_status_prefix}_failed" status for manual review.
    """
    with get_cursor() as cur:
        cur.execute("SELECT retry_count FROM companies WHERE cin = %s", (cin,))
        row = cur.fetchone()
        if row is None:
            return
        new_retry_count = row["retry_count"] + 1

        if new_retry_count >= MAX_RETRIES:
            cur.execute(
                """UPDATE companies
                   SET status = %s, retry_count = %s, last_error = %s,
                       updated_at = NOW()
                   WHERE cin = %s""",
                (f"{stage_status_prefix}_failed", new_retry_count, error_msg, cin),
            )
        else:
            cur.execute(
                """UPDATE companies
                   SET retry_count = %s, last_error = %s, updated_at = NOW()
                   WHERE cin = %s""",
                (new_retry_count, error_msg, cin),
            )


def mark_not_a_lead(cin, reason="already has a CMP"):
    """
    Terminal state for companies that fail the consent-gap check. Keeping
    this as its own status (not a failure) is what stops Apify/Apollo from
    ever being called on them -- the cost discipline lives in the state
    machine itself, not in application logic scattered across scripts.
    """
    advance_status(cin, "not_a_lead", last_error=reason)


def mark_excluded(cin, reason):
    """
    Terminal state for companies ruled out by policy rather than by the
    consent-gap check -- kept apart from 'not_a_lead', which every report reads
    as "already has a CMP".
    """
    advance_status(cin, "excluded", last_error=reason)


# Column order used by the stage-01 bulk insert. Everything else on the row
# (status, retry_count, timestamps) comes from the schema defaults.
INGEST_COLUMNS = (
    "cin",
    "company_name",
    "roc_code",
    "category",
    "subcategory",
    "company_class",
    "authorized_capital",
    "paidup_capital",
    "registration_date",
    "registered_address",
    "listing_status",
    "company_status",
    "state_code",
    "indian_foreign",
    "nic_code",
    "industrial_classification",
)


def bulk_insert_discovered(rows, page_size=1000):
    """
    Bulk-inserts MCA rows at the schema default status ('discovered') and
    returns how many were actually written.

    Idempotent by CIN: ON CONFLICT DO NOTHING means re-running stage 01 on the
    same file inserts zero rows rather than duplicating, and a row already
    enriched by a later stage is never clobbered by a re-ingest.

    `rows` is a list of dicts keyed by INGEST_COLUMNS; missing keys become NULL.
    """
    if not rows:
        return 0

    values = [tuple(r.get(c) for c in INGEST_COLUMNS) for r in rows]
    query = (
        f"INSERT INTO companies ({', '.join(INGEST_COLUMNS)}) VALUES %s "
        "ON CONFLICT (cin) DO NOTHING"
    )

    inserted = 0
    with get_cursor() as cur:
        # Chunked explicitly rather than leaning on execute_values' own paging:
        # cur.rowcount only reflects the final page, so the count would be wrong.
        for i in range(0, len(values), page_size):
            chunk = values[i:i + page_size]
            psycopg2.extras.execute_values(cur, query, chunk, page_size=len(chunk))
            inserted += cur.rowcount
    return inserted


def domain_owner(domain, exclude_cin=None):
    """
    The CIN that already claims this resolved_domain, or None.

    Two different CINs are two legally distinct companies, so they are never
    merged -- but group companies routinely share one website, and letting both
    through means Apify and Apollo are billed twice for the same site and often
    the same person. Stage 02 uses this to let the first claimant through and
    park the rest at 'duplicate_domain'.
    """
    if not domain:
        return None
    query = "SELECT cin FROM companies WHERE resolved_domain = %s"
    params = [domain]
    if exclude_cin:
        query += " AND cin <> %s"
        params.append(exclude_cin)
    query += " ORDER BY cin LIMIT 1"

    with get_cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        return row["cin"] if row else None


def all_company_names(limit=None):
    """Every company name, for building the generic-first-word table."""
    query = "SELECT company_name FROM companies"
    params = []
    if limit:
        query += " LIMIT %s"
        params.append(limit)
    with get_cursor() as cur:
        cur.execute(query, params)
        return [r["company_name"] for r in cur.fetchall()]


def count_by_status():
    """Quick dashboard: how many companies are sitting at each stage right now."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM companies GROUP BY status ORDER BY n DESC"
        )
        return {r["status"]: r["n"] for r in cur.fetchall()}


def _serialize_json_fields(fields):
    """
    List/dict values (open roles, cookie names, etc.) are wrapped in psycopg2's
    Json adapter so they land in their JSONB columns as real JSON, and come
    back out of get_pending() as Python lists/dicts -- no json.loads() needed
    on the read side. Scalars pass through untouched.
    """
    out = {}
    for k, v in fields.items():
        if isinstance(v, (list, dict)):
            out[k] = psycopg2.extras.Json(v)
        else:
            out[k] = v
    return out


if __name__ == "__main__":
    init_db()
    print("Database ready.")
    print("Status counts:", count_by_status())
