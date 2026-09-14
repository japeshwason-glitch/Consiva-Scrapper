"""
Scores the domain we already picked for companies resolved before the identity
check existed (resolve_match_score IS NULL).

    python backfill_match_score.py --dry-run
    python backfill_match_score.py --limit 50

This does NOT redo resolution. It re-fetches the stored domain and asks the
verifier one question: does this site's content match what the company name
says the business does? That is the check those rows never got, and it is the
one that keeps marvel.com out of MARVEL KING LIMITED's row.

Scores are written; status is left alone. crm_push already refuses anything
below resolver.ACCEPT_SCORE, so a low score parks the lead rather than
deleting work someone may want to look at.
"""
import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))

import db
import location
import resolver
import fingerprints
from importlib import import_module

_stage02 = import_module("02_resolve_domain")
fetch = _stage02.fetch

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--limit", type=int, default=200)
ap.add_argument("--workers", type=int, default=6)
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--status", default="exported,scraped",
                help="comma-separated statuses to score. Defaults to the two "
                     "that can still become leads; resolve_failed rows keep a "
                     "domain but were already rejected, so scoring them spends "
                     "model calls on a decision nobody will act on.")
args = ap.parse_args()
statuses = [s.strip() for s in args.status.split(",") if s.strip()]

with db.get_cursor() as cur:
    cur.execute("""
        SELECT cin, company_name, registered_address, industrial_classification,
               resolved_domain, final_domain, consent_gap_score, status
        FROM companies
        WHERE resolve_match_score IS NULL
          AND COALESCE(final_domain, resolved_domain) IS NOT NULL
          AND status = ANY(%s)
        ORDER BY consent_gap_score DESC NULLS LAST
        LIMIT %s""", (statuses, args.limit))
    todo = [dict(r) for r in cur.fetchall()]

if not todo:
    sys.exit("nothing to backfill -- every resolved company already has a score")

print(f"{len(todo)} compan(ies) to score\n")


def score_one(c):
    host = c["final_domain"] or c["resolved_domain"]
    got = fetch(host)
    if not got:
        return c, None, f"could not fetch {host}"

    _final_url, title, body, status = got
    if fingerprints.is_parked(title, body):
        return c, 0, "parked or placeholder page"
    if fingerprints.is_blocked(title, body, status):
        return c, None, f"blocked by a bot wall (HTTP {status})"

    score, reason = resolver.match_score_for(c, title, body)
    if score is None:
        return c, None, reason

    loc = location.parse(c.get("registered_address"))
    loc_score, loc_hit = resolver.location_axis(loc, f"{title} {body}",
                                                c["company_name"])
    if loc_score >= resolver.LOCATION_STRONG:
        score = min(100, score + resolver.CORROBORATION_BONUS)
        reason = f"{reason} (corroborated by {loc_hit})"
    return c, score, reason


passed = failed = unknown = 0
with ThreadPoolExecutor(max_workers=args.workers) as pool:
    futures = [pool.submit(score_one, c) for c in todo]
    for fut in as_completed(futures):
        c, score, reason = fut.result()
        host = c["final_domain"] or c["resolved_domain"]
        if score is None:
            unknown += 1
            verdict = "SKIP"
        elif score >= resolver.ACCEPT_SCORE:
            passed += 1
            verdict = "PASS"
        else:
            failed += 1
            verdict = "FAIL"
        print(f"{verdict} {str(score):>4}  {host:<34} {c['company_name'][:40]}")
        print(f"            {reason[:110]}")

        if score is not None and not args.dry_run:
            with db.get_cursor() as cur:
                cur.execute("""UPDATE companies
                               SET resolve_match_score = %s,
                                   resolve_verified_by = 'backfill',
                                   updated_at = NOW()
                               WHERE cin = %s""", (score, c["cin"]))

print(f"\npassed {passed} | failed {failed} | unscored {unknown}")
if args.dry_run:
    print("dry run -- nothing written")
