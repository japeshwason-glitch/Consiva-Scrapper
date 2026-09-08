"""
Stage 4: scraped (WHERE has_cmp=false) -> linkedin_done / linkedin_failed
Only ever queries companies that already cleared the consent-gap filter --
this is where the cost discipline actually lives.

    python stages/04_enrich_linkedin_apify.py --limit 20
    python stages/04_enrich_linkedin_apify.py --limit 20 --dry-run

LinkedIn data is ENRICHMENT, not a gate. It sharpens stage 06's Apollo persona
filters (seniority, headcount band, industry) and nothing more. A company with
no LinkedIn page still advances -- being absent from LinkedIn says nothing about
whether the company has a consent gap worth selling against.

Matching is verified before anything is written: LinkedIn's own `website` field
is compared against the domain stage 02 resolved. That is a deterministic
confirmation the two records are the same company, and it held on every match
in testing.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apify_client
import db
import llm
import location
import resolver

BATCH = 10          # the actor takes a list; one run for ten costs far less than ten runs
MATCH_MIN_SCORE = 60


def search_name(company_name):
    """
    LinkedIn indexes trading names, not registered ones. "Pestrail Chemicals
    Private Limited" finds nothing; the distinctive head of the name is what
    matches, exactly as with domain slugs.
    """
    cleaned = resolver.clean_name(company_name)
    words = [w for w in cleaned.split() if w not in resolver.STOPWORDS] or cleaned.split()
    return " ".join(words[:2]).title() if words else company_name


def domain_of(url):
    if not url:
        return ""
    host = re.sub(r"^https?://", "", str(url)).split("/")[0].lower()
    return host.removeprefix("www.")


def verify(company, record):
    """
    (accepted, reason). Website match is conclusive; HQ state is good
    corroboration; otherwise the model reads the description.
    """
    site = domain_of(record.get("website"))
    ours = (company.get("resolved_domain") or "").lower().removeprefix("www.")
    final = (company.get("final_domain") or "").lower().removeprefix("www.")

    if site and site in (ours, final):
        return True, f"LinkedIn website {site} matches resolved domain"

    hq = apify_client.headquarters(record) or {}
    loc = location.parse(company.get("registered_address"))
    area = (hq.get("geographicArea") or "").lower()
    if loc.get("state") and area and loc["state"] in area:
        # State agreement alone is weak -- confirm the business too.
        score, why = llm.match_score(
            company["company_name"], record.get("name") or "",
            record.get("description") or "",
            industry=company.get("industrial_classification"))
        if score is not None and score >= MATCH_MIN_SCORE:
            return True, f"HQ in {area}; description match {score}"
        return False, f"HQ state matches but description scored {score}: {why}"

    score, why = llm.match_score(
        company["company_name"], record.get("name") or "",
        record.get("description") or "",
        industry=company.get("industrial_classification"))
    if score is not None and score >= MATCH_MIN_SCORE:
        return True, f"description match {score}"
    return False, f"no website or location match; description scored {score}"


def size_band(record):
    rng = record.get("employeeCountRange") or {}
    start, end = rng.get("start"), rng.get("end")
    if start is None:
        return None
    return f"{start}-{end}" if end else f"{start}+"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-gap", type=int, default=1,
                    help="only enrich leads scoring at least this")
    args = ap.parse_args()

    companies = db.get_pending(
        "scraped", limit=args.limit,
        extra_where="has_cmp = false AND consent_gap_score >= %s",
        extra_params=(args.min_gap,))
    if not companies:
        print("nothing at status='scraped' with a consent gap")
        return
    print(f"enriching {len(companies)} leads via LinkedIn "
          f"(gap >= {args.min_gap})\n")

    enriched = unmatched = failed = 0

    for start in range(0, len(companies), BATCH):
        chunk = companies[start:start + BATCH]
        queries = {search_name(c["company_name"]): c for c in chunk}
        try:
            records = apify_client.search_companies(list(queries))
        except apify_client.ApifyUnavailable as exc:
            print(f"  apify unavailable: {exc}", flush=True)
            for company in chunk:
                failed += 1
                if not args.dry_run:
                    db.mark_failed(company["cin"], "linkedin", str(exc)[:300])
            continue

        # originalQuery comes back as {"search": ..., "location": ...}, not a
        # bare string, so the search term has to be dug out to re-associate a
        # result with the company that asked for it.
        by_query = {}
        for record in records:
            oq = record.get("originalQuery")
            term = oq.get("search") if isinstance(oq, dict) else (oq or "")
            by_query.setdefault(str(term), []).append(record)

        for query, company in queries.items():
            name = company["company_name"][:40]
            candidates = by_query.get(query) or []
            chosen, why = None, "no LinkedIn page found"
            for record in candidates:
                ok, reason = verify(company, record)
                if ok:
                    chosen, why = record, reason
                    break
                why = reason

            if chosen is None:
                # Absent from LinkedIn is not a failure: the lead still stands
                # on its consent gap. Advance with the fields left null.
                unmatched += 1
                print(f"  --    {name:<42} {why[:52]}", flush=True)
                if not args.dry_run:
                    db.advance_status(company["cin"], "linkedin_done",
                                      last_error=f"linkedin: {why[:300]}")
                continue

            hq = apify_client.headquarters(chosen) or {}
            hq_text = ", ".join(x for x in (hq.get("city"), hq.get("geographicArea")) if x)
            industries = [i.get("name") for i in (chosen.get("industries") or []) if i.get("name")]
            enriched += 1
            print(f"  OK    {name:<42} emp={chosen.get('employeeCount')} "
                  f"{industries[:1]} {hq_text[:26]}", flush=True)
            if not args.dry_run:
                db.advance_status(
                    company["cin"], "linkedin_done",
                    linkedin_employee_count=chosen.get("employeeCount"),
                    linkedin_industry=industries[0] if industries else None,
                    linkedin_hq=hq_text or None,
                    linkedin_size_band=size_band(chosen),
                    linkedin_website=chosen.get("website"),
                    linkedin_url=chosen.get("linkedinUrl"))

    print(f"\nenriched {enriched}, no LinkedIn match {unmatched}, failed {failed}")
    if args.dry_run:
        print("dry run -- nothing written")
    else:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
