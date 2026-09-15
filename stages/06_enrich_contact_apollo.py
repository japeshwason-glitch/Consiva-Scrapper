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
# Persona families from the CEO's ICP workbook. Title CLUSTERS, never exact
# matches -- Indian job titles vary too much for that.
#
# Base scores say who generally decides. Industry shifts which family leads:
# a SaaS company's CTO outranks its founder once a real technology function
# exists, while a D2C brand is still founder-run.
TITLE_PRIORITY = (
    # Founder / CEO
    (("founder", "co-founder", "cofounder", "promoter"), 100),
    (("chief executive", "ceo", "managing director", " md ", "mng director"), 95),
    (("proprietor", "partner", "owner"), 90),
    (("director",), 85),
    # CTO / Technology
    (("chief technology", "cto", "head of technology", "technology director",
      "vp engineering", "head of engineering", "head it", "head of it"), 80),
    # CIO / IT
    (("chief information officer", "cio", "it director", "chief digital officer",
      "chief digital"), 78),
    # DPO / Privacy -- highest-relevance persona for this product
    (("data protection officer", "dpo", "chief privacy officer", "privacy officer",
      "head privacy", "head of privacy", "privacy lead"), 88),
    # CISO / Security
    (("chief information security", "ciso", "head infosec", "head of infosec",
      "head cyber security", "head of cyber", "security director",
      "chief security"), 76),
    # Legal / Compliance
    (("general counsel", "chief legal officer", "head legal", "head of legal",
      "legal director", "legal head", "chief compliance", "head compliance",
      "head of compliance", "compliance officer", "compliance manager",
      "compliance head", "company secretary", "risk officer"), 74),
    # COO / Operations
    (("chief operating", "coo", "head operations", "head of operations",
      "operations director"), 70),
    # President / VP tier
    (("president", "vice president", " vp ", "avp"), 68),
    # General management
    (("general manager", " gm ", "gm&", "country manager", "business head"), 66),
    # Product / Digital
    (("chief product officer", "vp product", "head product", "head of product",
      "digital head", "head digital", "head of digital"), 64),
    # Marketing -- owns the website and its tags at most SMEs
    (("marketing head", "head of marketing", "vp marketing", "cmo",
      "marketing manager", "digital marketing", "brand manager"), 60),
    # HR / People -- employee-data owner
    (("chro", "hr director", "head hr", "head of hr", "hr operations",
      "people operations"), 52),
    # IT operations
    (("it manager", "it head", "system administrator", "webmaster",
      "technical head", "plant head"), 48),
    # Procurement -- gatekeeper, find after the primary buyer
    (("chief procurement", "head procurement", "head of procurement",
      "procurement director"), 40),
    # Bare "CPO" is ambiguous -- the workbook uses it for Chief Product Officer
    # AND Chief Procurement Officer, and it is also Chief Privacy Officer in the
    # wild. Scored mid-range so it is reachable but never outranks an
    # unambiguous title; the spelled-out forms above win on their own merits.
    (("cpo",), 50),
    (("manager",), 35),          # last resort -- better than nobody
)

# Industry overlay. Values may be NEGATIVE: the workbook is explicit that in
# regulated sectors the CIO/CISO leads and the founder does not, so a boost
# alone is not enough -- an unqualified founder scores 100 and would still win.
#
# Demotion is safe because the ladder picks the best AVAILABLE person: if a
# company has no CISO, the founder is still chosen. It only changes the outcome
# when both exist, which is exactly when the workbook says it should.
INDUSTRY_PERSONA_BOOST = {
    # BFSI / Insurance: regulated override, CIO/CISO/compliance lead
    "Finance": {
        ("cio", "chief information officer", "ciso", "chief information security",
         "compliance", "legal", "general counsel", "company secretary",
         "risk officer", "data protection", "dpo"): 20,
        ("founder", "co-founder", "chief executive", "ceo",
         "managing director"): -18,
    },
    "Insurance": {
        ("cio", "ciso", "chief information security", "compliance", "legal",
         "data protection", "dpo"): 20,
        ("founder", "co-founder", "chief executive", "ceo"): -18,
    },
    # SaaS / IT sits inside MCA's "Business Services": CTO outranks founder
    # once a real technology function exists.
    "Business Services": {
        ("cto", "chief technology", "head of technology", "vp engineering"): 18,
        ("founder", "co-founder", "chief executive", "ceo"): -8,
    },
    "Transport, storage and Communications": {
        ("cio", "it director", "head of it", "it head", "chief information"): 12,
    },
    "Community, personal and Social Services": {
        ("chief operating", "coo", "head operations"): 10,
    },
    # Owner-run sectors: leave the founder on top.
    "Trading": {
        ("founder", "chief executive", "ceo", "proprietor", "partner",
         "director"): 8,
    },
}


def title_score(title, employees, industry=None):
    """
    How good a target this person is. Industry shifts which persona family
    leads -- a bank's CISO outranks its founder; a trading firm's owner does
    not. Headcount still demotes founders at large companies where they are
    unreachable, but it is optional now that LinkedIn enrichment is off.
    """
    t = f" {(title or '').lower()} "
    score = 0
    for needles, points in TITLE_PRIORITY:
        if any(n in t for n in needles):
            score = max(score, points)

    if score:   # never promote a title the ladder did not recognise at all
        for needles, adjust in (INDUSTRY_PERSONA_BOOST.get(industry) or {}).items():
            if any(n in t for n in needles):
                score = max(0, score + adjust)
                break
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

    industry = company.get("linkedin_industry") or company.get("industrial_classification")
    ranked = sorted(with_email,
                    key=lambda p: -title_score(p.get("title"), employees, industry))
    best = ranked[0]
    if title_score(best.get("title"), employees, industry) == 0:
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
        return False

    reveal = config.APOLLO_ALLOW_EMAIL_REVEAL
    print(f"finding contacts for {len(companies)} companies "
          f"(email reveal {'ON -- SPENDING CREDITS' if reveal else 'OFF, free search only'})\n")

    found = missing = failed = 0
    budget_spent = False
    for company in companies:
        domain = company.get("final_domain") or company.get("resolved_domain")
        name = company["company_name"][:36]
        try:
            people, total = apollo_client.search_people(domain)
        except apollo_client.ApolloBudgetExhausted as exc:
            # The day's allowance is gone. Every company still in the list is
            # untouched at 'classified' and carries no retry charge, so the
            # next run picks up exactly here. Marking them failed would spend
            # their retries on our quota rather than on anything about them.
            print(f"\nSTOPPING: {str(exc)[:150]}", flush=True)
            print(f"  {len(companies) - (found + missing + failed)} compan(ies) "
                  f"left at 'classified' -- stage 07 exports them anyway, so "
                  f"they still reach the CRM today, just without a name",
                  flush=True)
            budget_spent = True
            break
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
    return budget_spent


if __name__ == "__main__":
    # 75 = EX_TEMPFAIL: nothing wrong with the data, the allowance is
    # spent. run_daily reads this and stops calling us until tomorrow.
    sys.exit(75 if main() else 0)
