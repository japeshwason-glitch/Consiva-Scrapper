"""
One-off: send drafted companies back through stage 9 (and then stage 10) after the email template or the drafting
prompt changed. A company at `drafted` or `written_to_crm` goes back to `verified`; stage 9 then replaces its
ae_drafts row (the upsert clears written_to_crm_at) and stage 10 writes the new draft over the AE_ columns it wrote
before. Scoped to the given CRM ids, production companies only, never `sent`. No CRM write happens here.

    python -m campaign.redraft --ids 1,2,3 [--dry-run]
"""
import argparse

import db

REASON = "re-opened for a new draft: template or prompt changed"


def redraft(ids, dry_run=False):
    with db.get_cursor() as cur:
        cur.execute("""SELECT company_id FROM ae_companies WHERE company_id = ANY(%s) AND NOT is_test
                       AND status IN ('drafted', 'written_to_crm') AND chosen_contact_id IS NOT NULL
                       ORDER BY company_id""", (ids,))
        found = [r["company_id"] for r in cur.fetchall()]
        if found and not dry_run:
            cur.execute("""UPDATE ae_companies SET status = 'verified', status_reason = %s, updated_at = NOW()
                           WHERE company_id = ANY(%s) AND status IN ('drafted', 'written_to_crm')""", (REASON, found))
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", required=True, type=lambda v: [int(x) for x in v.split(",") if x.strip()])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    got = redraft(a.ids, a.dry_run)
    print(f"{'would re-open' if a.dry_run else 're-opened'} {len(got)} companies for a new draft")
