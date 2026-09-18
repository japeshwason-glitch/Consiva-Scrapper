"""
Exports the companies whose website could not be guessed from their registered
name, as a worklist for a search-based pass.

    python export_no_site.py                  # writes data/exports/
    python export_no_site.py --limit 5000

These are NOT companies without a website. Every candidate hostname we generate
from the registered name answered NXDOMAIN, which mostly means the company
trades under a different name -- an abbreviation, a brand, a coinage. Measured
on a random 2,000: 368 of 2,000 records land here, against 906 that DID have a
live site we rejected as belonging to someone else.
"""
import argparse
import csv
import sys
from datetime import date
from pathlib import Path

import db



def main():
    FIELDS = ("cin", "company_name", "registered_address", "reg_state",
              "industrial_classification", "company_class", "registration_date")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=10000)
    ap.add_argument("--out", help="output path (default: data/exports/)")
    args = ap.parse_args()

    rows = db.get_pending("no_site_found", limit=args.limit)
    if not rows:
        sys.exit("nothing at status='no_site_found'")

    path = Path(args.out) if args.out else (
        Path(__file__).parent / "data" / "exports" /
        f"no_site_found_{date.today()}.csv")
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in FIELDS})

    print(f"wrote {len(rows)} rows -> {path}")



if __name__ == "__main__":
    main()
