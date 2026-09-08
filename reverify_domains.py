"""
Re-checks domains already accepted by stage 02, using the local model.

Rows resolved before the verifier existed were accepted on rapidfuzz alone at a
55 threshold. On the first 300-company batch that let through domains that were
confidently wrong -- MARVEL KING LIMITED -> marvel.com (Marvel Entertainment,
101 cookies), GAURAV SPINNERS -> gaurav.com ("Gaurav Jain", a personal site) --
and because wrong domains are usually big tracker-heavy sites, they scored
HIGHEST on consent_gap_score and would have reached Apollo first.

    python reverify_domains.py --dry-run     # report only
    python reverify_domains.py               # demote the failures

A row whose domain the model rejects is moved to 'resolve_failed' with the
reason recorded. Its scrape findings stay on the row for audit; nothing is
deleted.
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3

import db
import fingerprints
import llm

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TRUST_CONFIDENCE = 85.0  # matches stage 02's VERIFY_CEILING


def page_text(html, limit=4000):
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return title, re.sub(r"\s+", " ", body).strip()[:limit]


def check(company):
    """Returns (verdict, detail). verdict True=keep, False=demote, None=unknown."""
    domain = company["resolved_domain"]
    confidence = company["resolve_confidence"] or 0.0

    if confidence >= TRUST_CONFIDENCE:
        return True, f"confidence {confidence} >= {TRUST_CONFIDENCE}, not re-checked"

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    html = None
    for scheme in ("https://", "http://"):
        try:
            r = session.get(f"{scheme}{domain}", timeout=(5, 12),
                            allow_redirects=True, verify=False)
            if r.status_code < 400 and r.text:
                html = r.text
                break
        except requests.RequestException:
            continue
    if html is None:
        return None, "page unreachable now -- left as is"

    title, body = page_text(html)
    if fingerprints.is_parked(title, body, domain):
        return False, f"parked/placeholder page: {title[:70]!r}"

    verdict = llm.is_company_site(
        company["company_name"], title, body,
        industry=company.get("industrial_classification"),
        state=company.get("state_code"),
    )
    if verdict is None:
        return None, "verifier unavailable"
    return verdict, f"title={title[:70]!r}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    companies = []
    for status in ("resolved", "scraped"):
        companies.extend(db.get_pending(status, limit=5000))
    companies = [c for c in companies if c.get("resolved_domain")]
    if not companies:
        print("nothing to re-verify")
        return

    print(f"re-verifying {len(companies)} rows "
          f"(skipping any already at confidence >= {TRUST_CONFIDENCE})\n")

    kept = demoted = skipped = unknown = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, c): c for c in companies}
        for future in as_completed(futures):
            company = futures[future]
            name = company["company_name"][:36]
            domain = (company["resolved_domain"] or "")[:26]
            try:
                verdict, detail = future.result()
            except Exception as exc:
                verdict, detail = None, f"{type(exc).__name__}: {exc}"

            if verdict is True:
                if "not re-checked" in detail:
                    skipped += 1
                else:
                    kept += 1
                    print(f"  KEEP    {name:<38} {domain:<28} {detail}", flush=True)
            elif verdict is False:
                demoted += 1
                gap = company.get("consent_gap_score")
                print(f"  DEMOTE  {name:<38} {domain:<28} "
                      f"gap={gap} conf={company['resolve_confidence']} {detail}", flush=True)
                if not args.dry_run:
                    db.advance_status(
                        company["cin"], "resolve_failed",
                        last_error=f"domain rejected on re-verification: {detail}",
                        resolve_verified_by="llm-rejected",
                    )
            else:
                unknown += 1
                print(f"  ?       {name:<38} {domain:<28} {detail}", flush=True)

    print(f"\nkept {kept}, demoted {demoted}, "
          f"skipped(high confidence) {skipped}, undetermined {unknown}")
    if args.dry_run:
        print("dry run -- nothing written")
    else:
        print("status counts:", db.count_by_status())


if __name__ == "__main__":
    main()
