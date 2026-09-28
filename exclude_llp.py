"""
Park every in-flight LLP at 'excluded' so no stage spends on it again.

    python exclude_llp.py            # mark them
    python exclude_llp.py --dry-run  # count only

LLPs stopped going to the CRM on Sep 24 (see db.LLP_SQL). New ones are
screened out before stage 02; this clears the ones already partway through.
'discovered' rows are left alone -- the screening filter never selects them,
and marking 300k untouched rows would be work for nothing. Idempotent: an
excluded row is at no stage's status, so a rerun finds nothing.
"""

import argparse

import db

REASON = "LLP -- excluded: no paid-up capital to size it, weak Apollo match"
IN_FLIGHT = ("blocked", "resolved", "scraped", "classified",
             "linkedin_done", "contact_done", "contact_failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total = 0
    for status in IN_FLIGHT:
        rows = db.get_pending(status, limit=1_000_000, extra_where=db.LLP_SQL)
        print(f"  {status:<16} {len(rows):>6}")
        if not args.dry_run:
            for row in rows:
                db.mark_excluded(row["cin"], REASON)
        total += len(rows)
    print(f"{'would exclude' if args.dry_run else 'excluded'} {total} LLP(s)")
    if not args.dry_run:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
