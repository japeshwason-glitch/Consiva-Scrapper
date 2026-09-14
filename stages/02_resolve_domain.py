"""
Stage 2: discovered -> resolved / duplicate_domain / resolve_failed

Identity resolution, and nothing else. Name -> candidate hostnames -> direct
HTTP probe (no search engine) -> the local model judges whether the page's
CONTENT is what the company name says the business is.

    python stages/02_resolve_domain.py --limit 100
    python stages/02_resolve_domain.py --limit 20 --dry-run --verbose

Why the model decides rather than string similarity: the company name states
the line of business -- ELOHI FOODS sells food, DIAMOND HOMETEX makes home
textiles -- and only the page content can confirm that. String matching scores
marvel.com a perfect match for MARVEL KING LIMITED, and elohi.in a perfect
match for ELOHI FOODS while being an empty placeholder.

Nothing about cookies happens here. A site that fails identity never reaches
stage 03, so we never compute a consent gap for a company we do not have.
"""

import argparse
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
import location
import resolver

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

REQUEST_TIMEOUT = (4, 6)   # connect, read -- a site slower than this is not usable
MAX_WORKERS = 6           # each company makes model calls; do not stampede the server
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch(host):
    """
    (final_url, title, body) or None.

    A 4xx/5xx response with a real body is KEPT, not discarded. naughtyninos.com
    returns HTTP 402 (suspended Shopify store) with 10KB of page still naming
    the shop -- dropping it lost the only correct candidate that company had,
    leaving naughty.com as the best remaining option.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # One attempt per scheme, TLS verification off throughout. The old code
    # tried verify=True then verify=False for each scheme -- four attempts at
    # 15s apiece, so one slow host could burn 60 seconds. That made run-to-run
    # variance larger than any effect we were trying to measure.
    for scheme in ("https://", "http://"):
        try:
            r = session.get(f"{scheme}{host}", timeout=REQUEST_TIMEOUT,
                            allow_redirects=True, verify=False)
            if r.text and r.text.strip():
                return (r.url, *page_text(r.text), r.status_code)
        except requests.RequestException:
            continue
    return None


def page_text(html, limit=4000):
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return title, re.sub(r"\s+", " ", body).strip()[:limit]


def playwright_fetch_factory():
    """
    Returns a fetch_fn backed by a real browser, for hosts that a plain HTTP
    request cannot get past. Bot walls key off TLS fingerprints and JS
    execution, so requests loses where Chromium walks through. Used only on
    the blocked minority -- it is roughly 20x slower per page.
    """
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    def fetch_via_browser(host):
        for scheme in ("https://", "http://"):
            context = browser.new_context(
                user_agent=USER_AGENT, ignore_https_errors=True, locale="en-IN")
            page = context.new_page()
            try:
                response = page.goto(f"{scheme}{host}", wait_until="domcontentloaded",
                                     timeout=25000)
                page.wait_for_timeout(2500)
                html = page.content()
                status = response.status if response else 200
                final = page.url
                context.close()
                if html:
                    return (final, *page_text(html), status)
            except Exception:
                context.close()
                continue
        return None

    def close():
        browser.close()
        pw.stop()

    fetch_via_browser.close = close
    return fetch_via_browser


def resolve_one(company, verbose=False):
    loc = location.parse(company.get("registered_address"))
    logger = (lambda line: print(line, flush=True)) if verbose else None
    if verbose:
        print(f"  probing {company['company_name'][:50]}", flush=True)
    return resolver.resolve(company, fetch, loc=loc, log=logger)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true", help="probe but write nothing")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--verbose", action="store_true", help="show every candidate scored")
    ap.add_argument("--where", help="extra SQL filter, e.g. \"state_code='karnataka'\" "
                                   "-- lets a run target one state or industry")
    ap.add_argument("--no-escalate", action="store_true",
                    help="skip the browser pass over bot-walled hosts")
    args = ap.parse_args()

    companies = db.get_pending("discovered", limit=args.limit,
                               extra_where=args.where)
    if not companies:
        print("nothing at status='discovered'")
        return
    print(f"resolving {len(companies)} companies "
          f"(accept at match_score >= {resolver.ACCEPT_SCORE})\n")

    resolved = failed = duplicate = done = skipped_dns = 0
    claimed = {}
    blocked_companies = []

    def record(company, winner, reason, escalated=False):
        nonlocal resolved, failed, duplicate, skipped_dns
        name = company["company_name"][:42]

        if winner is None:
            # Hold blocked companies back for the browser pass rather than
            # writing them off on evidence we never actually saw.
            if reason.startswith("BLOCKED:") and not escalated:
                blocked_companies.append(company)
                print(f"  wall  {name:<44} {reason[:64]}", flush=True)
                return
            if reason.startswith("DNS_UNAVAILABLE:"):
                # We never got to ask about this company. Charging it a retry
                # would spend its three lives on our own broken resolver, and
                # during an outage that happens to every company at once.
                skipped_dns += 1
                print(f"  dns?  {name:<44} {reason[:64]}", flush=True)
                return
            failed += 1
            print(f"  none  {name:<44} {reason[:64]}", flush=True)
            if not args.dry_run:
                db.mark_failed(company["cin"], "resolve", reason[:400])
            return

        # Group companies share a website; only one should reach the paid stages.
        owner = claimed.get(winner["domain"]) or db.domain_owner(
            winner["domain"], exclude_cin=company["cin"])
        if owner:
            duplicate += 1
            note = f"shares {winner['domain']} with {owner}"
            print(f"  dup   {name:<44} {note}", flush=True)
            if not args.dry_run:
                db.advance_status(
                    company["cin"], "duplicate_domain",
                    resolved_domain=winner["domain"], tld_used=winner["tld"],
                    resolve_match_score=winner["score"], last_error=note)
            return

        resolved += 1
        claimed[winner["domain"]] = company["cin"]
        print(f"  OK    {name:<44} {winner['domain']:<30} "
              f"{winner['score']:>3}  {winner['reason'][:44]}", flush=True)
        if not args.dry_run:
            db.advance_status(
                company["cin"], "resolved",
                resolved_domain=winner["domain"], tld_used=winner["tld"],
                resolve_confidence=float(winner["score"]),
                resolve_match_score=winner["score"],
                resolve_evidence=winner["reason"][:500],
                resolve_verified_by="llm-match-score")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(resolve_one, c, args.verbose): c for c in companies}
        for future in as_completed(futures):
            company = futures[future]
            try:
                winner, reason, _ = future.result()
            except resolver.DNSUnavailable as exc:
                print(f"\nABORTING: {exc}", flush=True)
                print("No company was marked failed. Fix DNS and re-run; "
                      "every row is still at 'discovered'.", flush=True)
                pool.shutdown(wait=False, cancel_futures=True)
                sys.exit(75)   # EX_TEMPFAIL -- the work is retryable
            except Exception as exc:
                winner, reason = None, f"{type(exc).__name__}: {str(exc)[:180]}"
            record(company, winner, reason)
            done += 1
            if done % 25 == 0:
                print(f"  ... {done}/{len(companies)} "
                      f"({resolved} resolved, {duplicate} dup, {failed} none)",
                      flush=True)

    # Second pass: anything stopped by a bot wall gets a real browser. These
    # are companies we could not LOOK at, not companies we judged wrong.
    if blocked_companies and not args.no_escalate:
        print(f"\nescalating {len(blocked_companies)} blocked companies to a browser",
              flush=True)
        browser_fetch = playwright_fetch_factory()
        try:
            for company in blocked_companies:
                loc = location.parse(company.get("registered_address"))
                try:
                    winner, reason, _ = resolver.resolve(company, browser_fetch, loc=loc)
                except resolver.DNSUnavailable as exc:
                    # Same rule as the main pass: a broken resolver is not a
                    # verdict. Stop here rather than charging retries to the
                    # companies we have not reached yet.
                    print(f"\nABORTING escalation: {exc}", flush=True)
                    break
                except Exception as exc:
                    winner, reason = None, f"{type(exc).__name__}: {str(exc)[:150]}"
                record(company, winner, reason, escalated=True)
        finally:
            browser_fetch.close()

    print(f"\nresolved {resolved}, duplicate-domain {duplicate}, "
          f"no site found {failed} of {len(companies)}")
    if skipped_dns:
        print(f"NOTE: {skipped_dns} compan(ies) skipped -- their lookups could not "
              f"be completed. Left at 'discovered' with no retry charged.")
    if args.dry_run:
        print("dry run -- nothing written")
    else:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
