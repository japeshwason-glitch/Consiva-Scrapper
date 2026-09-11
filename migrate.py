"""
Applies SQL migrations in order, once each.

    python migrate.py              # apply anything outstanding
    python migrate.py --status     # list applied / pending
    python migrate.py --dry-run

Migrations live in migrations/ as NNN_name.sql and run in filename order inside
a transaction. Applied versions are recorded in schema_migrations, so this is
safe to run on every deploy -- it is a no-op when nothing is outstanding.
"""

import argparse
import hashlib
import sys
from pathlib import Path

import psycopg2

import config

MIGRATIONS = Path(__file__).resolve().parent / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def files():
    return sorted(MIGRATIONS.glob("*.sql"), key=lambda p: p.name)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set (see .env.example)")

    conn = psycopg2.connect(config.DATABASE_URL)
    cur = conn.cursor()
    cur.execute(BOOTSTRAP)
    conn.commit()

    cur.execute("SELECT version, checksum FROM schema_migrations")
    applied = dict(cur.fetchall())

    pending = []
    for path in files():
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        if path.stem in applied:
            if applied[path.stem] != checksum:
                # A migration that changed after being applied means the schema
                # on this database no longer matches the file. Refuse rather
                # than guess which is right.
                sys.exit(f"{path.name} has changed since it was applied. "
                         f"Add a new migration instead of editing an old one.")
            continue
        pending.append((path, checksum))

    if args.status:
        print(f"applied: {len(applied)}")
        for v in sorted(applied):
            print(f"  ok      {v}")
        for path, _ in pending:
            print(f"  PENDING {path.stem}")
        return

    if not pending:
        print("schema is up to date")
        return

    for path, checksum in pending:
        print(f"applying {path.name} ...", end=" ", flush=True)
        if args.dry_run:
            print("(dry run)")
            continue
        try:
            cur.execute(path.read_text())
            cur.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (path.stem, checksum))
            conn.commit()
            print("ok")
        except Exception as exc:
            conn.rollback()
            sys.exit(f"FAILED\n  {exc}")

    print(f"{len(pending)} migration(s) applied")


if __name__ == "__main__":
    main()
