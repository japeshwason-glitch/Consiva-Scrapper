"""
Re-measure delivered leads that stage 03 read before it could see shadow DOM.

    python recheck_banners.py --domain atss.in          # check one site, write nothing
    python recheck_banners.py --scan data/recheck.json  # every delivered no-banner lead
    python recheck_banners.py --apply data/recheck.json # fix our DB and CRM_Scrapper

Until Sep 24 stage 03 read page.content(), which leaves out shadow roots.
WPConsent moves its whole banner into one, and it is a real CMP besides, so
those companies were delivered as "no banner, no CMP" when they had both.

--scan loads each homepage once with the fixed reading and records only the
companies whose answer changed, re-running the full stage-03 measurement on
those so the score and summary come from one consistent read. It writes
nothing. --apply then, per company:
  * CMP found, or the new score is no longer a gap -> 'not_a_lead' here, row
    deleted from CRM_Scrapper. Not a lead; sales should not be calling them.
  * banner found, still a gap -> banner, score and summary corrected here and
    CookieBanner / GapScore / ConsentFinding updated in CRM_Scrapper.
Every CRM row touched is saved to data/backups/ first.

CRM_Scrapper has no CIN, so a row is matched on CompanyName + Website -- the
two values crm_push wrote.
"""

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))

from playwright.async_api import async_playwright

import config
import crm_push
import db
import fingerprints

s3 = import_module("03_scrape_playwright")

CONCURRENCY = 8
BACKUP_DIR = Path(__file__).resolve().parent / "data" / "backups"


async def screen(browser, domain):
    """Homepage only, the fixed stage-03 reading: (cmp_name, banner)."""
    context = await browser.new_context(
        user_agent=s3.USER_AGENT, viewport={"width": 1366, "height": 900},
        ignore_https_errors=True, locale="en-IN")
    try:
        try:
            page, html, _ = await s3.load(context, f"https://{domain}")
        except Exception:
            page, html, _ = await s3.load(context, f"http://{domain}")
        shadow = await page.evaluate(s3.SHADOW_JS)
        scripts = await page.evaluate(
            "() => Array.from(document.querySelectorAll('script[src]')).map(s => s.src)")
        blob = "\n".join([html, shadow, " ".join(scripts)])
        return fingerprints.detect_cmp(blob), fingerprints.detect_banner(blob)
    finally:
        await context.close()


async def remeasure(browser, company):
    """The full stage-03 consent measurement, without the model."""
    result, _ = await s3.scrape_one(browser, company, use_llm=False)
    return {k: result[k] for k in ("has_cmp", "cmp_name", "has_cookie_banner",
                                   "consent_gap_score", "consent_gap_summary")}


async def check_one(domain):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        cmp_name, banner = await screen(browser, domain)
        print(f"{domain}: cmp={cmp_name} banner={banner}")
        full = await remeasure(browser, {"cin": "-", "company_name": domain.split(".")[0],
                                         "resolved_domain": domain})
        print(json.dumps(full, indent=2))
        await browser.close()


async def scan(out_path):
    leads = db.get_pending("exported", limit=100_000,
                           extra_where="delivered_at IS NOT NULL AND has_cmp = false "
                                       "AND has_cookie_banner = false")
    print(f"re-checking {len(leads)} delivered no-banner leads", flush=True)
    flips, errors, done = [], 0, 0
    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def one(c):
            nonlocal errors, done
            async with sem:
                domain = c.get("final_domain") or c["resolved_domain"]
                try:
                    cmp_name, banner = await screen(browser, domain)
                    if cmp_name or banner:
                        new = await remeasure(browser, c)
                        flips.append({"cin": c["cin"], "company_name": c["company_name"],
                                      "website": c.get("final_domain") or c.get("resolved_domain"),
                                      "old_score": c["consent_gap_score"], **new})
                        print(f"  FLIP  {domain:<30} cmp={new['cmp_name']} "
                              f"banner={new['has_cookie_banner']} "
                              f"score {c['consent_gap_score']} -> {new['consent_gap_score']}",
                              flush=True)
                except Exception as exc:
                    errors += 1
                    print(f"  skip  {domain:<30} {str(exc)[:60]}", flush=True)
                done += 1
                if done % 100 == 0:
                    print(f"  ... {done}/{len(leads)}", flush=True)

        await asyncio.gather(*(one(c) for c in leads))
        await browser.close()

    Path(out_path).write_text(json.dumps(flips, indent=1, default=str))
    print(f"\n{len(flips)} changed, {errors} unreachable, of {len(leads)} -> {out_path}")


def apply(in_path):
    flips = json.loads(Path(in_path).read_text())
    # The screen and the full re-measure are two separate page loads, and a
    # banner can render on one and not the other (popt.ai did). Act only on
    # what the full measurement confirms -- it is the one --apply writes.
    unconfirmed = [f for f in flips if not (f["has_cmp"] or f["has_cookie_banner"])]
    flips = [f for f in flips if f not in unconfirmed]
    if unconfirmed:
        print(f"{len(unconfirmed)} not confirmed on re-measure, left alone: "
              + ", ".join(f["website"] for f in unconfirmed))
    drop =[f for f in flips if f["has_cmp"] or not fingerprints.is_lead_score(f["consent_gap_score"])]
    fix = [f for f in flips if f not in drop]
    print(f"{len(drop)} to remove (CMP or no gap), {len(fix)} to correct")

    conn = crm_push.connect()
    cur = conn.cursor()
    table = config.CRM_DB_TABLE
    match = "CompanyName = %s AND Website = %s"

    # Back up every CRM row about to change, before anything is written.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"crm_scrapper_recheck_{datetime.now():%Y-%m-%d_%H%M}.csv"
    with backup.open("w", newline="") as fh:
        w = None
        for f in flips:
            cur.execute(f"SELECT * FROM {table} WHERE {match}", (f["company_name"], f["website"]))
            for row in cur.fetchall():
                if w is None:
                    w = csv.writer(fh)
                    w.writerow([d[0] for d in cur.description])
                w.writerow(row)
    print(f"backed up affected CRM rows -> {backup}")

    removed = updated = 0
    for f in drop:
        reason = f["cmp_name"] or "no consent gap on re-check"
        cur.execute(f"DELETE FROM {table} WHERE {match}", (f["company_name"], f["website"]))
        removed += cur.rowcount
        db.advance_status(f["cin"], "not_a_lead", has_cmp=f["has_cmp"], cmp_name=f["cmp_name"],
                          has_cookie_banner=f["has_cookie_banner"],
                          consent_gap_score=f["consent_gap_score"],
                          consent_gap_summary=f["consent_gap_summary"],
                          last_error=f"recheck: {reason}")
    for f in fix:
        cur.execute(f"UPDATE {table} SET CookieBanner = %s, GapScore = %s, ConsentFinding = %s, "
                    f"UpdatedAt = GETDATE() WHERE {match}",
                    ("present", f["consent_gap_score"], f["consent_gap_summary"],
                     f["company_name"], f["website"]))
        updated += cur.rowcount
        db.advance_status(f["cin"], "exported", has_cookie_banner=True,
                          consent_gap_score=f["consent_gap_score"],
                          consent_gap_summary=f["consent_gap_summary"])
    conn.commit()
    conn.close()
    print(f"CRM: {removed} row(s) deleted, {updated} row(s) corrected")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--domain", help="check one site and print what stage 03 now sees")
    g.add_argument("--scan", metavar="OUT.json")
    g.add_argument("--apply", metavar="IN.json")
    args = ap.parse_args()
    if args.domain:
        asyncio.run(check_one(args.domain))
    elif args.scan:
        asyncio.run(scan(args.scan))
    else:
        apply(args.apply)


if __name__ == "__main__":
    main()
