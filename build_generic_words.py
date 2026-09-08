"""
Builds the generic-first-word table from the registry itself.

A word that leads several company names cannot identify any one of them.
Deriving this from the data beats hand-listing: an English dictionary has no
"shree", "shri", "jai" or "krishna", yet each leads 10-43 Haryana companies.

    python build_generic_words.py [--min 3]
"""
import argparse, collections, json
from pathlib import Path
import db, resolver

ap = argparse.ArgumentParser()
ap.add_argument("--min", type=int, default=resolver.GENERIC_MIN_COMPANIES,
                help="a first word shared by this many companies is generic")
args = ap.parse_args()

counts = collections.Counter()
names = db.all_company_names()
for name in names:
    words = [w for w in resolver.clean_name(name).split() if w not in resolver.STOPWORDS]
    if words:
        counts[words[0].lower()] += 1

generic = sorted(w for w, n in counts.items() if n >= args.min)
path = Path(resolver.GENERIC_WORDS_PATH)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(generic, indent=0))
print(f"{len(names)} company names -> {len(counts)} distinct first words")
print(f"{len(generic)} are generic (>= {args.min} companies) -> {path}")
print("sample:", generic[:18])
