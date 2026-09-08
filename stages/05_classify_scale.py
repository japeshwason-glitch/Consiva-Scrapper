"""
Stage 5: linkedin_done -> classified
Rule-based, free, instant. Combines paid-up capital + Apify employee count +
NIC code into MSME / SMB / Startup / Enterprise.

    python stages/05_classify_scale.py --limit 100
    python stages/05_classify_scale.py --limit 100 --dry-run

No LLM and no external call, deliberately: this feeds stage 06's persona
filters and stage 07's export, and a human needs to be able to audit why a
company was banded the way it was. `scale_basis` names the signals that
actually drove the decision -- including when the only signal was capital,
because LinkedIn had no page for the company.

team_headcount_site is not used: it came back null on essentially every site
scraped, and LinkedIn is the real source for headcount.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

CRORE = 10_000_000

# Paid-up capital is not the MSME Act's "investment in plant and machinery",
# but it is the only capital figure MCA gives us, and it bands companies
# sensibly in practice.
ENTERPRISE_CAPITAL = 50 * CRORE
SMB_CAPITAL = 10 * CRORE
MSME_CAPITAL = 1 * CRORE

ENTERPRISE_EMPLOYEES = 250
SMB_EMPLOYEES = 50

STARTUP_MAX_AGE_YEARS = 5
STARTUP_MAX_EMPLOYEES = 50


def years_since(registration_date):
    if not registration_date:
        return None
    try:
        year, month, day = (int(x) for x in str(registration_date)[:10].split("-"))
    except ValueError:
        return None
    today = date.today()
    return (today - date(year, month, day)).days / 365.25


def classify(company):
    """(business_scale, scale_confidence, scale_basis)."""
    capital = company.get("paidup_capital")
    employees = company.get("linkedin_employee_count")
    age = years_since(company.get("registration_date"))
    industry = company.get("linkedin_industry") or company.get("industrial_classification")

    signals, scale = [], None

    if employees is not None:
        signals.append(f"{employees} employees on LinkedIn")
        if employees >= ENTERPRISE_EMPLOYEES:
            scale = "Enterprise"
        elif employees >= SMB_EMPLOYEES:
            scale = "SMB"

    if capital is not None:
        signals.append(f"paid-up capital Rs {capital / CRORE:.2f} cr")
        if scale is None:
            if capital >= ENTERPRISE_CAPITAL:
                scale = "Enterprise"
            elif capital >= SMB_CAPITAL:
                scale = "SMB"
        elif scale != "Enterprise" and capital >= ENTERPRISE_CAPITAL:
            # Capital can promote, never demote: a lean company with heavy
            # capital is still an enterprise buyer.
            scale = "Enterprise"
            signals.append("capital overrides headcount")

    if scale is None and age is not None and age <= STARTUP_MAX_AGE_YEARS:
        if employees is None or employees < STARTUP_MAX_EMPLOYEES:
            scale = "Startup"
            signals.append(f"incorporated {age:.1f} years ago")

    if scale is None:
        scale = "MSME"

    if age is not None and scale != "Startup":
        signals.append(f"{age:.0f} years old")
    if industry:
        signals.append(f"industry: {industry}")

    # Confidence reflects what we actually had, not how neat the answer looks.
    if employees is not None and capital is not None:
        confidence = "high"
    elif employees is not None or capital is not None:
        near_boundary = capital is not None and (
            0.7 * SMB_CAPITAL <= capital <= 1.3 * SMB_CAPITAL
            or 0.7 * ENTERPRISE_CAPITAL <= capital <= 1.3 * ENTERPRISE_CAPITAL)
        confidence = "low" if near_boundary else "moderate"
    else:
        confidence = "low"
        signals.append("no headcount or capital available")

    return scale, confidence, "; ".join(signals)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    companies = db.get_pending("linkedin_done", limit=args.limit)
    if not companies:
        print("nothing at status='linkedin_done'")
        return
    print(f"classifying {len(companies)} companies\n")

    counts = {}
    for company in companies:
        scale, confidence, basis = classify(company)
        counts[scale] = counts.get(scale, 0) + 1
        print(f"  {scale:<11} {confidence:<9} {company['company_name'][:38]:<40} {basis[:56]}",
              flush=True)
        if not args.dry_run:
            db.advance_status(company["cin"], "classified",
                              business_scale=scale,
                              scale_confidence=confidence,
                              scale_basis=basis[:500])

    print(f"\n{counts}")
    if args.dry_run:
        print("dry run -- nothing written")
    else:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
