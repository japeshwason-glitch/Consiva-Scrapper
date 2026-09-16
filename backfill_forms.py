"""
Re-measures companies delivered before form detection existed, and corrects
both databases in place.

    python backfill_forms.py --dry-run
    python backfill_forms.py

33 rows reached the CRM with has_data_form NULL -- never measured. Their gap
scores understate the exposure, because collecting personal data without a
consent checkbox is part of the score. This re-runs stage 03's analysis on
exactly those and updates:

  * our companies row, keeping its status (they are already delivered)
  * the matching dbo.CRM_Scrapper row, matched on Website -- their table has
    no CIN column, so the domain is the only key we share

It never inserts. A company already sent stays one row.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))
from importlib import import_module

import config
import crm_push
import db

_s3 = import_module("03_scrape_playwright")

# What the CRM row carries that a form finding can change.
CRM_FIELDS = ("GapScore", "TrackersPreConsent", "ThirdPartiesCount",
              "CookieBanner", "CollectsViaForm", "ConsentFinding")


async def measure(companies, concurrency, use_llm):
    from playwright.async_api import async_playwright
    out, sem = {}, asyncio.Semaphore(concurrency)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            async def one(c):
                async with sem:
                    try:
                        data, err = await _s3.scrape_one(browser, c, use_llm)
                        return c["cin"], (data if data else {"error": err})
                    except Exception as exc:
                        return c["cin"], {"error": f"{type(exc).__name__}: {exc}"[:180]}
            for cin, data in await asyncio.gather(*(one(c) for c in companies)):
                out[cin] = data
        finally:
            await browser.close()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with db.get_cursor() as cur:
        cur.execute("""SELECT cin, company_name, status, consent_gap_score,
                              COALESCE(final_domain, resolved_domain) AS resolved_domain
                       FROM companies
                       WHERE delivered_at IS NOT NULL AND has_data_form IS NULL
                       ORDER BY updated_at LIMIT %s""", (args.limit,))
        companies = [dict(r) for r in cur.fetchall()]

    if not companies:
        print("nothing to backfill -- every delivered company has form data")
        return
    print(f"re-measuring {len(companies)} delivered compan(ies) with no form data\n")
    if args.dry_run:
        for c in companies:
            print(f"  would re-measure {c['resolved_domain']:<32} "
                  f"(gap {c['consent_gap_score']})")
        print("\ndry run -- nothing written")
        return

    results = asyncio.run(measure(companies, args.concurrency, not args.no_llm))
    conn = crm_push.connect()
    cur = conn.cursor()
    changed = forms = failed = 0

    for c in companies:
        data = results.get(c["cin"]) or {}
        if data.get("error") or "consent_gap_score" not in data:
            failed += 1
            print(f"  FAIL  {c['resolved_domain']:<32} {data.get('error','no data')[:40]}")
            continue

        before = c["consent_gap_score"]
        after = data["consent_gap_score"]
        form = crm_push.form_sentence(data)
        if data.get("has_data_form"):
            forms += 1

        # Our row: same status, corrected findings.
        fields = {k: data[k] for k in _s3.SCRAPE_FIELDS}
        db.advance_status(c["cin"], c["status"], **fields)

        # Their row, matched on the only key both tables share.
        cur.execute(f"""UPDATE {config.CRM_DB_TABLE}
                        SET GapScore=%s, TrackersPreConsent=%s, ThirdPartiesCount=%s,
                            CookieBanner=%s, CollectsViaForm=%s, ConsentFinding=%s,
                            UpdatedAt=SYSUTCDATETIME()
                        WHERE Website=%s""",
                    (after, data["tracking_cookie_count"], data["third_party_count"],
                     "present" if data["has_cookie_banner"] else "none",
                     form or None, data["consent_gap_summary"], c["resolved_domain"]))
        conn.commit()
        changed += 1
        move = "same" if after == before else f"{before} -> {after}"
        print(f"  OK    {c['resolved_domain']:<32} gap {move:<12} "
              f"{form[:34] if form else 'no form'}")

    conn.close()
    print(f"\n{changed} corrected, {forms} of them collect data via a form, "
          f"{failed} unreachable")


if __name__ == "__main__":
    main()
