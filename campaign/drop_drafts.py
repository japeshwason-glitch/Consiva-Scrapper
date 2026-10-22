"""
One-off: take drafts out of circulation. A drafted or written draft becomes `expired`, which is the pipeline's own "nobody sends
this" status, in Postgres (ae_companies and ae_drafts) and, for a draft already written to the CRM, in AE_DraftStatus.

    python -m campaign.drop_drafts --ids 20900,25226 --reason "junk company name" [--dry-run]

The CRM write goes through crm_write.update_company(only_if_unsent=True): a draft that has been sent is never touched (it is
reported as skipped_sent and nothing changes for it). Test companies, companies that are not at drafted / written_to_crm, and ids
that are not in ae_companies are skipped and reported. Nothing is deleted: the draft text stays where it was.
"""
import argparse
import sys
from collections import Counter

import psycopg2.extras

import db

from . import crm_write, flags, locks, status

DROPPABLE = ("drafted", "written_to_crm")


def drop(ids, reason, dry_run=False, log=print):
    out = {}
    for cid in ids:
        out[cid] = _drop_one(cid, reason, dry_run)
        log(f"  {cid}: {out[cid]}")
    log(f"done: {dict(Counter(out.values()))}{' (dry run: nothing was changed)' if dry_run else ''}")
    return out


def _drop_one(cid, reason, dry_run):
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status, is_test FROM ae_companies WHERE company_id = %s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if row is None:
                return "skipped_unknown"
            if row["is_test"]:
                return "skipped_test"
            if row["status"] not in DROPPABLE:
                return f"skipped_status_{row['status']}"
            if dry_run:
                return "would_drop_in_crm" if row["status"] == "written_to_crm" else "would_drop"
            was_written = row["status"] == "written_to_crm"
            cur.execute("UPDATE ae_drafts SET draft_status = 'expired' WHERE company_id = %s", (cid,))
            status.set_status(cur, cid, "expired", reason)
            if was_written and not crm_write.update_company(cid, only_if_unsent=True, allow_held_out=True,
                                                            AE_DraftStatus="expired"):
                conn.rollback()
                return "skipped_sent"
    return "dropped_in_crm" if was_written else "dropped"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", required=True, type=lambda v: [int(x) for x in v.split(",") if x.strip()])
    ap.add_argument("--reason", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if flags.kill_switch_on():
        print("kill switch is on: not running")
        return 1
    with locks.job_lock("drop_drafts") as got:
        if not got:
            print("another drop_drafts run holds the lock")
            return 1
        drop(args.ids, args.reason, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
