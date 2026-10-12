"""
One run of a job at a time, across processes and containers: a session-level
Postgres advisory lock named after the job. It is held on its own dedicated
connection for as long as the job runs; if the process dies, Postgres drops the
connection and the lock with it, so a crash never leaves a stale lock.
"""
from contextlib import contextmanager

import psycopg2

import config


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
