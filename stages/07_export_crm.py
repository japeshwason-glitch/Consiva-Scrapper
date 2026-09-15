"""
Stage 7: contact_done / contact_failed / classified (with a gap) -> exported
Pulls the 11 outward-facing fields only -- raw facts, no written hooks --
into a CSV for the CRM person.

    python stages/07_export_crm.py                    # writes data/exports/
    python stages/07_export_crm.py --dry-run          # preview, nothing written

The field list is fixed by CLAUDE.md and deliberately narrow: everything here
is something we observed, not something a model composed. consent_gap_summary
is assembled deterministically in stage 03 from the cookies and forms actually
found, so the CRM person can quote it without checking it.
"""

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

FIELDS = (
    "company_name",
    "domain",
    "business_scale",
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_source",
    "title_match_confidence",
    "consent_gap_summary",
    "company_tagline",
    "notable_open_roles",
)


def row_for(company):
    roles = company.get("notable_open_roles") or []
    if isinstance(roles, str):
        try:
            roles = json.loads(roles)
        except ValueError:
            roles = [roles]
    return {
        "company_name": company.get("company_name"),
        # the domain we actually landed on, which is what the CRM person visits
        "domain": company.get("final_domain") or company.get("resolved_domain"),
        "business_scale": company.get("business_scale"),
        "contact_name": company.get("contact_name"),
        "contact_title": company.get("contact_title"),
        "contact_email": company.get("contact_email"),
        "contact_source": company.get("contact_source"),
        "title_match_confidence": company.get("title_match_confidence"),
        "consent_gap_summary": company.get("consent_gap_summary"),
        "company_tagline": company.get("company_tagline"),
        "notable_open_roles": "; ".join(str(r) for r in roles) if roles else "",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--out", help="output path (default data/exports/<date>.csv)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # A lead is a company with a consent gap. Whether a contact was found for
    # it -- or whether we even got to ask -- does not change that.
    #
    # contact_done / contact_failed are the two outcomes of stage 06. Gating on
    # contact_done alone exported 5 of 48 real leads, because Apollo has no
    # record of most small Indian companies.
    #
    # 'classified' is here because stage 06 stops when its daily allowance is
    # spent, leaving companies at that status. Without this they never export,
    # never reach the CRM, and never count toward the daily target -- while
    # still showing as pending work, so run_daily keeps cycling and delivers
    # nothing. Contact enrichment is additive, never a gate.
    gap = "has_cmp = false AND consent_gap_score > 0"
    companies = []
    for status in ("contact_done", "contact_failed", "classified"):
        companies += db.get_pending(status, limit=args.limit, extra_where=gap)
    companies = companies[:args.limit]
    if not companies:
        print("nothing awaiting export (need a consent gap and no CMP)")
        return

    rows = [row_for(c) for c in companies]
    missing_email = sum(1 for r in rows if not r["contact_email"])
    missing_contact = sum(1 for r in rows if not r["contact_name"])

    if args.dry_run:
        print(",".join(FIELDS))
        for r in rows:
            print(",".join('"' + str(r[f] or "").replace('"', '""') + '"' for f in FIELDS))
        print(f"\n{len(rows)} rows -- dry run, nothing written")
    else:
        path = Path(args.out or f"data/exports/consiva_leads_{date.today()}.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        for company in companies:
            db.advance_status(company["cin"], "exported")
        print(f"wrote {len(rows)} rows -> {path}")
        print("status counts:", db.count_by_status())

    if missing_contact:
        print(f"NOTE: {missing_contact} of {len(rows)} rows have no contact name "
              f"(Apollo has no record of them -- sales sources via Lusha)")
    if missing_email:
        print(f"NOTE: {missing_email} of {len(rows)} rows have no contact_email "
              f"(Apollo free plan -- reveal disabled)")


if __name__ == "__main__":
    main()
