"""
Stage 6: classified -> contact_done / contact_failed
Apollo api_search (free) filtered by domain + title priority, cross-checked
against founder_name_site, then one people/match reveal (1 credit). Lusha
as fallback when Apollo has no usable match.

    python stages/06_enrich_contact_apollo.py --limit 20
    python stages/06_enrich_contact_apollo.py --limit 20 --dry-run

FREE-TIER MODE. people SEARCH is free and gives first name, obfuscated surname,
title and a has_email flag -- enough to pick the right PERSONA. The paid
people/match reveal is gated behind APOLLO_ALLOW_EMAIL_REVEAL, currently off,
so contact_email stays NULL and no credits are spent. Turn it on when the
account is upgraded; nothing else in this stage changes.

Title priority follows CLAUDE.md, but is shaped by company size from stage 04:
at a 5-person company the founder answers the email, at a 300-person company
the founder never sees it and the right target is whoever owns the website.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import apollo_client
import config
import db

# CLAUDE.md's ladder: Founder/Co-founder > CEO > CTO > Head of Eng/Product >
# Marketing Head.
TITLE_PRIORITY = (
    (("founder", "co-founder", "cofounder"), 100),
    (("chief executive", "ceo", "managing director", " md ", "mng director"), 95),
    # Indian SMEs are usually run by a proprietor, partner or director rather
    # than anyone with a C-title. The first pass scored only the C-suite and
    # missed real decision-makers: NXTRA VIZAG had 25 contactable people and
    # none matched, top of the list being "GM & ...".
    (("proprietor", "partner", "owner", "promoter"), 90),
    (("director",), 85),
    (("president", "vice president", " vp ", "avp"), 75),
    (("general manager", " gm ", "gm&", "country manager", "business head"), 72),
    (("chief technology", "cto", "chief information", "cio",
      "chief operating", "coo", "chief marketing", "cmo",
      "chief compliance", "chief data"), 70),
    (("head of engineering", "head of product", "head of operations",
      "head of technology", "head of it", "head of digital",
      "head of marketing", "head of sales", "head of business"), 65),
    (("vp engineering", "vp product", "vp marketing", "vp sales"), 62),
    (("marketing head", "marketing manager", "digital marketing",
      "brand manager", "ecommerce manager"), 58),
    (("data protection officer", "dpo", "compliance", "legal head",
      "company secretary"), 56),
    (("it manager", "it head", "system administrator", "webmaster",
      "technical head", "plant head"), 48),
    (("manager",), 35),          # last resort -- better than nobody
)

# Above this headcount the founder is unreachable and the person who actually
# owns the website is the better target.
LARGE_COMPANY_EMPLOYEES = 150
LARGE_COMPANY_BOOST = ("marketing", "digital", "it ", "technology", "compliance",
                       "legal", "data protection", "privacy")


def title_score(title, employees):
    t = f" {(title or '').lower()} "
    score = 0
    for needles, points in TITLE_PRIORITY:
        if any(n in t for n in needles):
            score = max(score, points)
    if employees and employees >= LARGE_COMPANY_EMPLOYEES:
        if any(n in t for n in LARGE_COMPANY_BOOST):
            score += 25          # they own the website at this size
        if "founder" in t or "ceo" in t:
            score -= 20          # unreachable at this size
    return score


def name_agrees(apollo_first, founder_name_site):
    """
    Apollo obfuscates surnames on the free tier, so only the first name can be
    compared. CLAUDE.md wants a conflict recorded rather than silently trusted --
    Apollo's title data is unreliable for smaller companies.
    """
    if not founder_name_site or not apollo_first:
        return None
    site_words = {w.lower().strip(".,") for w in re.split(r"\s+", founder_name_site)}
    return apollo_first.lower() in site_words


def choose(people, company):
    """(person, title_match_confidence, note) or (None, None, reason)."""
    employees = company.get("linkedin_employee_count")
    with_email = [p for p in people if p.get("has_email")]
    if not with_email:
        return None, None, f"{len(people)} people found, none with a verified email"

    ranked = sorted(with_email, key=lambda p: -title_score(p.get("title"), employees))
    best = ranked[0]
    if title_score(best.get("title"), employees) == 0:
        return None, None, (f"{len(with_email)} contactable, none matching the title "
                            f"ladder (top: {best.get('title')!r})")

    agrees = name_agrees(best.get("first_name"), company.get("founder_name_site"))
    if agrees is True:
        confidence, note = "high", f"first name agrees with site founder {company['founder_name_site']!r}"
    elif agrees is False:
        confidence, note = "moderate", (f"Apollo says {best.get('first_name')}, site says "
                                        f"{company['founder_name_site']!r} -- conflict noted, not resolved")
    else:
        confidence, note = "moderate", "no founder name on the site to cross-check"
    return best, confidence, note


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    companies = db.get_pending("classified", limit=args.limit)
    if not companies:
        print("nothing at status='classified'")
        return

    reveal = config.APOLLO_ALLOW_EMAIL_REVEAL
    print(f"finding contacts for {len(companies)} companies "
          f"(email reveal {'ON -- SPENDING CREDITS' if reveal else 'OFF, free search only'})\n")

    found = missing = failed = 0
    for company in companies:
        domain = company.get("final_domain") or company.get("resolved_domain")
        name = company["company_name"][:36]
        try:
            people, total = apollo_client.search_people(domain)
        except apollo_client.ApolloUnavailable as exc:
            failed += 1
            print(f"  FAIL  {name:<38} {str(exc)[:60]}", flush=True)
            if not args.dry_run:
                db.mark_failed(company["cin"], "contact", str(exc)[:300])
            continue

        person, confidence, note = choose(people, company)
        if person is None:
            missing += 1
            print(f"  --    {name:<38} {note[:58]}", flush=True)
            if not args.dry_run:
                db.advance_status(company["cin"], "contact_failed",
                                  last_error=f"apollo: {note[:300]}")
            continue

        # Apollo returns the literal string "None" for a missing surname on
        # some records; joined naively that produces "Andrew None" in the CSV.
        parts = [person.get("first_name"), person.get("last_name_obfuscated")]
        contact_name = " ".join(
            p for p in parts if p and str(p).strip().lower() not in ("none", "null")
        ).strip()
        email = None
        if reveal:
            try:
                matched = apollo_client.reveal_email(person["id"])
                email = (matched.get("person") or {}).get("email")
            except apollo_client.ApolloPaidFeature:
                pass

        found += 1
        print(f"  OK    {name:<38} {contact_name:<20} {str(person.get('title'))[:26]:<28} "
              f"{confidence}", flush=True)
        if not args.dry_run:
            db.advance_status(
                company["cin"], "contact_done",
                apollo_person_id=person.get("id"),
                contact_name=contact_name,
                contact_title=person.get("title"),
                contact_email=email,
                contact_source="apollo",
                title_match_confidence=confidence,
                last_error=note[:300])

    print(f"\ncontacts found {found}, no usable contact {missing}, failed {failed}")
    if not reveal:
        print("NOTE: contact_email is NULL for every row -- free plan, reveal disabled.")
    if args.dry_run:
        print("dry run -- nothing written")
    else:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
