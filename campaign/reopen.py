"""
One-off: re-open production companies that stage 2 skipped only because their contacts were on free-mail addresses,
after settings_checks.ALLOW_FREE_MAIL was switched on. Their free_mail contact results are cleared and the company
goes back to `pending`, so stage 2 checks them again under the new rule. Nothing else about them changes; no CRM
write happens (stage 2 never wrote one).

    python -m campaign.reopen [--dry-run] [--ids 1,2,3]
"""
import argparse

import db

from . import settings_checks as cfg

REASON = "re-opened: free-mail contacts are now allowed"


def reopen(ids=None, dry_run=False):
    if not cfg.ALLOW_FREE_MAIL:
        raise SystemExit("AE_ALLOW_FREE_MAIL is off: nothing to re-open")
    with db.get_cursor() as cur:
        cur.execute("""SELECT c.company_id FROM ae_companies c
                       WHERE c.status = 'skipped_invalid' AND NOT c.is_test
                         AND (%s::int[] IS NULL OR c.company_id = ANY(%s))
                         AND EXISTS (SELECT 1 FROM ae_contacts k
                                     WHERE k.company_id = c.company_id AND k.check_reason = 'free_mail')
                       ORDER BY c.company_id""", (ids, ids))
        found = [r["company_id"] for r in cur.fetchall()]
        if found and not dry_run:
            cur.execute("""UPDATE ae_contacts SET check_passed = NULL, check_reason = NULL, checked_at = NULL
                           WHERE company_id = ANY(%s) AND check_reason = 'free_mail'""", (found,))
            cur.execute("""UPDATE ae_companies SET status = 'pending', status_reason = %s, updated_at = NOW()
                           WHERE company_id = ANY(%s) AND status = 'skipped_invalid'""", (REASON, found))
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ids", type=lambda v: [int(x) for x in v.split(",") if x.strip()])
    a = ap.parse_args()
    got = reopen(a.ids, a.dry_run)
    print(f"{'would re-open' if a.dry_run else 're-opened'} {len(got)} companies")
