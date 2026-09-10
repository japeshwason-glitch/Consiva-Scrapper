"""
Merges several state MCA exports into one file for stage 01.

    python merge_mca.py --out data/raw/Metropolitan-MCA.csv

Two things happen on the way through:

* **Only Active companies are kept.** The raw state exports are roughly half
  struck-off, dissolved, amalgamated or under liquidation -- Chandigarh alone
  is 43% Strike Off. Those have no website and no ability to buy anything, and
  the Haryana file we already work from was pre-filtered the same way.

* **Deduplicated on canonical CIN** (whitespace stripped, uppercased), the same
  rule stage 01 uses. CINs should not repeat across states, but a company that
  re-registered or appears in two extracts would otherwise be scraped twice and
  enriched twice.

NIC codes are NOT filtered -- the target list is the user's call, and stage 01
can filter later without re-merging.
"""

import argparse
import collections
import csv
import re
import sys
from pathlib import Path

csv.field_size_limit(10_000_000)

COLUMNS = [
    "CIN", "CompanyName", "CompanyROCcode", "CompanyCategory",
    "CompanySubCategory", "CompanyClass", "AuthorizedCapital", "PaidupCapital",
    "CompanyRegistrationdate_date", "Registered_Office_Address", "Listingstatus",
    "CompanyStatus", "CompanyStateCode", "CompanyIndian/Foreign Company",
    "nic_code", "CompanyIndustrialClassification",
]
KEEP_STATUS = {"active"}


def canonical_cin(value):
    return re.sub(r"\s+", "", (value or "")).upper()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", default="data/raw/Metropolitan-MCA.csv")
    ap.add_argument("--keep-all-statuses", action="store_true",
                    help="do not filter to Active companies")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen = set()
    stats = collections.OrderedDict()
    status_dropped = collections.Counter()
    written = 0

    with out_path.open("w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(out_fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()

        for path in args.inputs:
            path = Path(path)
            if not path.exists():
                sys.exit(f"missing input: {path}")
            counts = {"read": 0, "kept": 0, "not_active": 0, "duplicate": 0, "no_cin": 0}

            with path.open(encoding="utf-8", errors="replace", newline="") as fh:
                reader = csv.DictReader(fh)
                missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
                if missing:
                    sys.exit(f"{path.name} is missing columns: {missing}")

                for row in reader:
                    counts["read"] += 1
                    cin = canonical_cin(row.get("CIN"))
                    if not cin:
                        counts["no_cin"] += 1
                        continue
                    status = (row.get("CompanyStatus") or "").strip().lower()
                    if not args.keep_all_statuses and status not in KEEP_STATUS:
                        counts["not_active"] += 1
                        status_dropped[row.get("CompanyStatus") or "(blank)"] += 1
                        continue
                    if cin in seen:
                        counts["duplicate"] += 1
                        continue
                    seen.add(cin)
                    row["CIN"] = cin
                    writer.writerow(row)
                    counts["kept"] += 1
                    written += 1

            stats[path.name] = counts
            print(f"  {path.name:<26} read {counts['read']:>8,}  "
                  f"kept {counts['kept']:>8,}  "
                  f"not-active {counts['not_active']:>8,}  "
                  f"dupes {counts['duplicate']:>5,}", flush=True)

    total_read = sum(c["read"] for c in stats.values())
    total_dupes = sum(c["duplicate"] for c in stats.values())
    total_inactive = sum(c["not_active"] for c in stats.values())
    print(f"\n  {'TOTAL':<26} read {total_read:>8,}  kept {written:>8,}  "
          f"not-active {total_inactive:>8,}  dupes {total_dupes:>5,}")
    print(f"\nwrote {out_path}  ({out_path.stat().st_size/1e6:.0f} MB)")
    if status_dropped:
        print("\ndropped by status:")
        for status, n in status_dropped.most_common(10):
            print(f"  {n:>8,}  {status}")


if __name__ == "__main__":
    main()
