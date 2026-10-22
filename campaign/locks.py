"""
One run of a job at a time, across processes and containers: a session-level
Postgres advisory lock named after the job. It is held on its own dedicated
connection for as long as the job runs; if the process dies, Postgres drops the
connection and the lock with it, so a crash never leaves a stale lock.
"""
from contextlib import contextmanager

import psycopg2

import config


def job_running(name):
    """
    True while some session holds the advisory lock that job_lock(name) takes. Read-only: it looks in pg_locks and
    never takes the lock itself, so asking can never make a scheduled job believe another run holds it.
    """
    conn = psycopg2.connect(config.DATABASE_URL)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("""SELECT EXISTS (SELECT 1 FROM pg_locks
                       WHERE locktype = 'advisory' AND granted AND objsubid = 1
                         AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                         AND ((classid::bigint << 32) | objid::bigint) = hashtext(%s)::bigint)""",
                    (f"ae_job:{name}",))
        return bool(cur.fetchone()[0])
    finally:
        conn.close()


@contextmanager
def job_lock(name):
    """Yields True if this process now holds the lock for `name`, False if another does."""
    conn = psycopg2.connect(config.DATABASE_URL)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"ae_job:{name}",))
        yield bool(cur.fetchone()[0])
    finally:
        conn.close()                     # closing the session releases the lock
