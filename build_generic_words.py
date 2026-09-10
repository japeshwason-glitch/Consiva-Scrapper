"""
Builds the generic-first-word table from the registry itself.

A word that leads a large share of company names cannot identify any one of
them. Deriving this from the data beats hand-listing: an English dictionary has
no "shree", "shri", "jai" or "krishna", yet each leads a meaningful slice of
Haryana companies.

    python build_generic_words.py                          # from the database
    python build_generic_words.py --csv data/raw/X.csv     # straight from a CSV
    python build_generic_words.py --share 0.002 # stricter
    python build_generic_words.py --dry-run

Runs at ~48,000 names/sec, so 1.7M rows takes well under a minute.
"""
import argparse
import collections
import json
import sys
from pathlib import Path

import db
import resolver

# If a run would flag more than this share of the vocabulary, something is
# wrong with the threshold and writing the file would cripple stage 02 --
# it would stop probing single-word domains entirely, silently.
SANITY_MAX_VOCAB_SHARE = 0.10

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--min-companies", type=int, default=resolver.GENERIC_MIN_COMPANIES,
                help="a word leading at least this many companies is generic")
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--csv", help="read CompanyName from this CSV instead of the database, "
                             "so the table can be built before ingest")
args = ap.parse_args()

if args.csv:
    import csv
    csv.field_size_limit(10_000_000)
    with open(args.csv, encoding="utf-8", errors="replace", newline="") as fh:
        names = [row["CompanyName"] for row in csv.DictReader(fh) if row.get("CompanyName")]
else:
    names = db.all_company_names()
if not names:
    sys.exit("no company names in the database -- ingest first")

counts = collections.Counter()
for name in names:
    words = [w for w in resolver.clean_name(name).split() if w not in resolver.STOPWORDS]
    if words:
        counts[words[0].lower()] += 1

cutoff = args.min_companies
generic = sorted(w for w, n in counts.items() if n >= cutoff)
vocab_share = len(generic) / len(counts) if counts else 0

print(f"{len(names):,} company names -> {len(counts):,} distinct first words")
print(f"cutoff: a word leading >= {cutoff:,} companies")
print(f"flagged generic: {len(generic):,} words "
      f"({vocab_share * 100:.1f}% of the vocabulary)")
print("sample:", generic[:16])

# Calibration check. These are words we have seen behave one way or the other
# on real runs; if the cutoff starts getting them wrong, it is mis-set.
KNOWN_JUNK = ("shree", "shri", "sai", "om", "jai", "maa", "shiv", "bharat",
              "krishna", "royal", "star", "super", "golden", "global", "new")
KNOWN_BRANDS = ("livguard", "nxtra", "pestrail", "aisats", "encalm",
                "zealthix", "kohinoor", "repin", "alektrify", "viok")
flagged = set(generic)
missed = [w for w in KNOWN_JUNK if counts.get(w, 0) and w not in flagged]
eaten = [w for w in KNOWN_BRANDS if w in flagged]
print(f"calibration: junk caught {len(KNOWN_JUNK)-len(missed)}/{len(KNOWN_JUNK)}"
      + (f" (missing: {missed})" if missed else "")
      + f" | brands wrongly flagged: {eaten or 'none'}")

if vocab_share > SANITY_MAX_VOCAB_SHARE:
    sys.exit(f"\nREFUSING TO WRITE: {vocab_share*100:.0f}% of the vocabulary was "
             f"flagged generic (limit {SANITY_MAX_VOCAB_SHARE*100:.0f}%).\n"
             f"Stage 02 would stop probing single-word domains almost entirely. "
             f"Raise --share and re-run.")

if args.dry_run:
    print("\ndry run -- nothing written")
else:
    path = Path(resolver.GENERIC_WORDS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generic, indent=0))
    print(f"-> {path}")
