"""
Stage 1: Ingest MCA CSV -> companies table, status='discovered'.
Input: a data.gov.in export already filtered to Active status + target NIC codes.

    python stages/01_ingest_mca.py data/raw/mca.xlsx
    python stages/01_ingest_mca.py data/raw/mca.xlsx --limit 50   # small test batch
    python stages/01_ingest_mca.py data/raw/mca.xlsx --dry-run    # parse only, no writes

Idempotent: re-running on the same file inserts nothing (ON CONFLICT DO NOTHING
on cin), so a crash partway through is fixed by simply running it again.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

# MCA export header -> schema column.
COLUMN_MAP = {
    "CIN": "cin",
    "CompanyName": "company_name",
    "CompanyROCcode": "roc_code",
    "CompanyCategory": "category",
    "CompanySubCategory": "subcategory",
    "CompanyClass": "company_class",
    "AuthorizedCapital": "authorized_capital",
    "PaidupCapital": "paidup_capital",
    "CompanyRegistrationdate_date": "registration_date",
    "Registered_Office_Address": "registered_address",
    "Listingstatus": "listing_status",
    "CompanyStatus": "company_status",
    "CompanyStateCode": "state_code",
    "CompanyIndian/Foreign Company": "indian_foreign",
    "nic_code": "nic_code",
    "CompanyIndustrialClassification": "industrial_classification",
}

TEXT_COLUMNS = (
    "cin", "company_name", "roc_code", "category", "subcategory",
    "company_class", "registered_address", "listing_status", "company_status",
    "state_code", "indian_foreign", "industrial_classification",
)
FLOAT_COLUMNS = ("authorized_capital", "paidup_capital")
INT_COLUMNS = ("nic_code",)

# Legal suffixes stripped only for *reporting* near-duplicate names. Never used
# to merge rows -- see dedupe() for why.
NAME_SUFFIXES = (
    "PRIVATE LIMITED", "PVT LTD", "PVT. LTD.", "LIMITED", "LTD", "LLP",
)


def normalise_cin(series):
    """
    CIN is the dedup key, so it has to be compared in a canonical form: MCA
    CINs are uppercase alphanumeric and exactly 21 characters. Without this,
    a stray space or a lowercased export slips past both the in-file dedup and
    the ON CONFLICT (cin) guard, and the same company lands twice.
    """
    return (
        series.astype("string")
        .str.replace(r"\s+", "", regex=True)
        .str.upper()
        .replace({"": pd.NA})
    )


def normalise_name(series):
    """Canonical company name, for detecting near-duplicates worth a human look."""
    out = (
        series.astype("string")
        .str.upper()
        .str.replace(r"[^A-Z0-9 ]", " ", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )
    for suffix in NAME_SUFFIXES:
        out = out.str.replace(rf"\s*{suffix}$", "", regex=True)
    return out.str.strip()


def load(path):
    """Reads the export. MCA ships these as .xlsx as often as .csv."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path, encoding="utf-8-sig")


def normalise(df):
    """
    Renames to schema columns and coerces types. Blanks become None (SQL NULL)
    rather than 0 or "" -- stage 05 classifies on paid-up capital, so a zero
    standing in for a missing value would quietly skew the scale decision.
    """
    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        raise SystemExit(f"Input is missing expected MCA columns: {missing}")

    df = df[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)

    df["cin"] = normalise_cin(df["cin"])

    for col in TEXT_COLUMNS:
        df[col] = (
            df[col].astype("string").str.strip().replace({"": pd.NA, "NA": pd.NA})
        )

    # 'CompanyIndian/Foreign Company' arrives as the string 'India' on some rows
    # and the integer 91 (India's country code) on others. Left alone, that
    # gives one concept two spellings and any later `indian_foreign = 'India'`
    # filter silently drops the 91-rows -- all of which carry Indian CINs.
    df["indian_foreign"] = df["indian_foreign"].replace({"91": "India"})

    for col in FLOAT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Float64")
    for col in INT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    # schema keeps registration_date as TEXT; ISO-8601 so it sorts correctly.
    dates = pd.to_datetime(df["registration_date"], errors="coerce")
    df["registration_date"] = dates.dt.strftime("%Y-%m-%d").astype("string")

    return df


def validate(df):
    """Drops rows that can't be inserted at all. CIN is the primary key."""
    report = {}

    no_cin = df["cin"].isna()
    report["missing CIN"] = int(no_cin.sum())
    df = df[~no_cin]

    # Companies carry a 21-character CIN; LLPs carry an LLPIN, which looks like
    # "ACS-4781". Both are valid registry identifiers and both are real
    # businesses with DPDP obligations -- a length check tuned to CINs alone
    # silently dropped 310,656 LLPs (21% of the metropolitan corpus).
    valid = df["cin"].str.fullmatch(r"[A-Z0-9]{21}|[A-Z]{2,4}-?\d{4,6}")
    malformed = ~valid.fillna(False)
    report["identifier is neither a CIN nor an LLPIN"] = int(malformed.sum())
    df = df[~malformed]

    no_name = df["company_name"].isna()
    report["missing CompanyName"] = int(no_name.sum())
    df = df[~no_name]

    return df, report


def dedupe(df):
    """
    Removes rows that are the same company twice.

    Dedup is on normalised CIN and nothing else. A CIN is MCA's unique
    registration number, so two rows sharing one are the same registered
    company; two rows with *different* CINs are two legally distinct
    companies even when their names match exactly, and collapsing those
    would delete real leads. Name collisions are therefore reported for a
    human to look at, never merged automatically.

    Keeps the first occurrence -- MCA exports are ordered oldest-first, so the
    original registration wins over any later restatement.
    """
    report = {}

    dupes = df["cin"].duplicated(keep="first")
    report["duplicate CIN within file"] = int(dupes.sum())
    if dupes.any():
        for cin in df.loc[dupes, "cin"].unique()[:5]:
            names = df.loc[df["cin"] == cin, "company_name"].tolist()
            print(f"    duplicate CIN {cin}: {names}")
    df = df[~dupes]

    # Reported, not dropped.
    canonical = normalise_name(df["company_name"])
    collisions = canonical.duplicated(keep=False) & canonical.notna()
    if collisions.any():
        groups = df[collisions].assign(_k=canonical[collisions]).groupby("_k")
        print(f"  note: {groups.ngroups} name collisions across distinct CINs "
              f"({int(collisions.sum())} rows) -- kept, not merged:")
        for key, grp in list(groups)[:5]:
            print(f"    {key!r}: {grp['cin'].tolist()}")

    return df, report


def to_records(df):
    """DataFrame -> list of dicts with real Python types and None for NULL."""
    return df.astype(object).where(pd.notna(df), None).to_dict("records")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="MCA export (.xlsx or .csv)")
    ap.add_argument("--limit", type=int, help="only ingest the first N rows")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    df = load(args.path)
    print(f"read {len(df)} rows from {args.path}")

    df = normalise(df)
    df, report = validate(df)
    df, dedup_report = dedupe(df)
    for reason, n in {**report, **dedup_report}.items():
        if n:
            print(f"  dropped {n} rows: {reason}")

    if args.limit:
        df = df.head(args.limit)
        print(f"  limited to first {len(df)} rows")

    records = to_records(df)
    if args.dry_run:
        print(f"dry run -- {len(records)} rows ready, nothing written")
        if records:
            print("sample:", {k: records[0][k] for k in list(records[0])[:6]})
        return

    db.init_db()
    before = db.count_by_status()
    inserted = db.bulk_insert_discovered(records)
    after = db.count_by_status()

    skipped = len(records) - inserted
    print(f"inserted {inserted} new rows at status='discovered'")
    if skipped:
        print(f"skipped {skipped} rows already present (ON CONFLICT DO NOTHING)")
    print(f"status counts: {before} -> {after}")


if __name__ == "__main__":
    main()
