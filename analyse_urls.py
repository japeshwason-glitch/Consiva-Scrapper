"""
Analyses a hand-supplied list of URLs and puts them through the normal lead
path into dbo.CRM_Scrapper.

    python analyse_urls.py https://www.example.com https://another.in
    python analyse_urls.py --dry-run <url>

The MCA scraper starts from a company name and has to find the website. This
starts from the website, so there is no registry row behind it -- no CIN, no
paid-up capital, no NIC code. Scale and industry therefore come from the site
itself, the same way the inbound job handles them.

Rows land in our companies table under a synthetic identifier so that
delivered_at still applies and a URL analysed twice is not sent twice. They
then go through stages 06 and 07 and crm_push unchanged.
"""
import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))
from importlib import import_module

import db
import llm
import personas

_s3 = import_module("03_scrape_playwright")

# Synthetic, and deliberately not CIN-shaped: these have no registry identity,
# and personas.is_llp must not read one into it.
CIN_PREFIX = "URL"


def host_of(url):
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


async def analyse(hosts, use_llm, concurrency):
    from playwright.async_api import async_playwright
    out, sem = {}, asyncio.Semaphore(concurrency)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            async def one(host):
                async with sem:
                    company = {"resolved_domain": host, "company_name": host,
                               "cin": f"{CIN_PREFIX}:{host}"}
                    try:
                        data, err = await _s3.scrape_one(browser, company, use_llm)
                        return host, (data if data else {"error": err})
                    except Exception as exc:
                        return host, {"error": f"{type(exc).__name__}: {exc}"[:200]}
            for host, data in await asyncio.gather(*(one(h) for h in hosts)):
                out[host] = data
        finally:
            await browser.close()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    hosts = []
    for u in args.urls:
        h = host_of(u)
        if h and h not in hosts:
            hosts.append(h)
    print(f"analysing {len(hosts)} site(s): {', '.join(hosts)}\n")

    results = asyncio.run(analyse(hosts, not args.no_llm, args.concurrency))

    for host, data in results.items():
        if data.get("error"):
            print(f"  FAIL  {host:<26} {data['error'][:56]}")
            continue

        name = host
        industry = scale = None
        if not args.no_llm:
            industry, scale, conf, why = llm.profile_company(
                host, host, data.get("company_tagline") or "", data)
        titles = personas.predict(scale=scale, industry=industry, cin=None)

        score = data["consent_gap_score"]
        verdict = ("has a CMP -- not a lead" if data["has_cmp"]
                   else "no gap" if score >= 100 else f"score {score}")
        print(f"  OK    {host:<26} {verdict:<24} {scale}/{industry}")
        print(f"          hunt: {' | '.join(titles)}")

        if args.dry_run:
            continue

        cin = f"{CIN_PREFIX}:{host}"
        fields = {k: data[k] for k in _s3.SCRAPE_FIELDS}
        fields.update(business_scale=scale, industrial_classification=industry,
                      resolved_domain=host, scale_basis="inferred from the site; "
                      "no registry row exists for a hand-supplied URL",
                      resolve_match_score=100,
                      resolve_evidence="URL supplied directly; no identity check needed")
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO companies (cin, company_name, status)
                           VALUES (%s, %s, 'discovered')
                           ON CONFLICT (cin) DO NOTHING""", (cin, name))
        # has_cmp companies stop here, exactly as they do in the MCA path.
        db.advance_status(cin, "not_a_lead" if data["has_cmp"] else "classified",
                          **fields)

    if args.dry_run:
        print("\ndry run -- nothing written")
        return
    print("\nwritten at status 'classified'. Next:")
    print("  python stages/06_enrich_contact_apollo.py --limit 10")
    print("  python stages/07_export_crm.py && python crm_push.py --limit 10")


if __name__ == "__main__":
    main()
