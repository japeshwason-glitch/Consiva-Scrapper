"""
Consent-gap analysis for INBOUND leads (job 2).

The MCA scraper guesses a company's website and then has to prove the guess is
right. This job has no such problem: the lead handed us a work email, so the
domain in it IS their company. No resolver, no identity check, no MCA.

    python inbound_analyse.py                    # today's leads
    python inbound_analyse.py --date 2026-09-14  # one day
    python inbound_analyse.py --days 7           # a backfill window
    python inbound_analyse.py --dry-run

Reads crm.CrmLeads, analyses the domain behind each lead's Email, and writes
the findings back onto the same row -- columns 36-47, the same shape the MCA
scraper sends to dbo.CRM_Scrapper. It is an UPDATE job; it never inserts a
lead, because the lead already exists.

Already-analysed rows are skipped on ConsentFinding, not GapScore: a genuine
score of 0 (a real site with no gap) has to stay distinguishable from "not
looked at yet".
"""
import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))
from importlib import import_module

import config
import crm_push

_stage03 = import_module("03_scrape_playwright")

# An email at one of these says nothing about a company website. Sending a
# crawler at gmail.com would measure Google's consent posture and attach it to
# a lead, which is worse than recording nothing.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "zoho.com", "zohomail.com", "yandex.com", "mail.com", "gmx.com",
    "rediffmail.com", "rediff.com", "indiatimes.com", "sify.com",
    "ymail.com", "rocketmail.com", "inbox.com", "fastmail.com",
    "hotmail.co.uk", "yahoo.co.uk", "outlook.in", "duck.com",
}

# Domains to leave alone regardless -- your own, a partner's, a test harness.
SKIP_DOMAINS = {d.strip().lower() for d in
                (getattr(config, "INBOUND_SKIP_DOMAINS", "") or "").split(",")
                if d.strip()}

# CrmLeads column <- analysis key. Deliberately the same 12 facts the MCA
# scraper sends, so both jobs produce one comparable record.
# consent_gap_score is already on the outward scale (100 = clean), so it maps
# straight across with no conversion.
COLUMN_MAP = (
    ("ScrapperWebsite",     "final_domain"),
    ("GapScore",            "consent_gap_score"),
    ("TrackersPreConsent",  "tracking_cookie_count"),
    ("ThirdPartiesCount",   "third_party_count"),
    ("ConsentFinding",      "consent_gap_summary"),
)


def domain_of(email):
    """
    Company domain from a work email, or None if it is not one.

    Handles plus-addressing (akansh+leadtest@swaransoft.com), which is all over
    the real data because QA uses it.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return None
    if domain in FREE_EMAIL_DOMAINS:
        return None
    if domain in SKIP_DOMAINS:
        return None
    return domain


def reason_skipped(email):
    if not email or "@" not in email:
        return "no email on the lead, nothing to analyse"
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if domain in FREE_EMAIL_DOMAINS:
        return (f"personal email domain ({domain}) -- no company website to "
                f"analyse; ask sales for the company URL")
    if domain in SKIP_DOMAINS:
        return f"{domain} is on the skip list"
    return f"could not derive a domain from {email}"


def fetch_leads(conn, since, until, force=False):
    """Leads created in the window that have not been analysed yet."""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT Id, Email, Company, FirstName, LastName, JobTitle, CreatedAt
        FROM crm.CrmLeads
        WHERE IsDeleted = 0
          AND CreatedAt >= %s AND CreatedAt < %s
          {"" if force else "AND ConsentFinding IS NULL"}
        ORDER BY CreatedAt""", (since, until))
    cols = ("Id", "Email", "Company", "FirstName", "LastName", "JobTitle", "CreatedAt")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def write_result(conn, lead_ids, data=None, note=None):
    """
    Write one analysis onto every lead that shares the domain.

    Several leads routinely arrive from the same company, and the consent
    posture of a website is a property of the website -- so it is measured once
    and applied to each, rather than crawling the same site five times.
    """
    if data:
        cols = [c for c, _ in COLUMN_MAP] + ["CookieBanner", "CollectsViaForm"]
        values = [data.get(k) for _, k in COLUMN_MAP] + [
            "present" if data.get("has_cookie_banner") else "none",
            crm_push.form_sentence(data) or None,
        ]
        sets = ", ".join(f"{c} = %s" for c in cols)
    else:
        sets, values = "ConsentFinding = %s", [note]
    sets += ", UpdatedAt = SYSUTCDATETIME()"

    cur = conn.cursor()
    for lead_id in lead_ids:
        cur.execute(f"UPDATE crm.CrmLeads SET {sets} WHERE Id = %s",
                    (*values, lead_id))
    conn.commit()


async def analyse(domains, use_llm, concurrency):
    """{domain: analysis dict or {'error': str}} -- one browser, N contexts."""
    from playwright.async_api import async_playwright
    out = {}
    sem = asyncio.Semaphore(concurrency)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            async def one(domain):
                async with sem:
                    # scrape_one takes the MCA row shape; the only fields it
                    # reads are the domain and a name for logging.
                    company = {"resolved_domain": domain, "company_name": domain,
                               "cin": f"inbound:{domain}"}
                    try:
                        # scrape_one returns (data, error): data on success,
                        # None plus a reason when the page could not be read.
                        data, err = await _stage03.scrape_one(
                            browser, company, use_llm)
                        return domain, (data if data else {"error": err})
                    except Exception as exc:
                        return domain, {"error": f"{type(exc).__name__}: {exc}"[:200]}

            for domain, data in await asyncio.gather(*(one(d) for d in domains)):
                out[domain] = data
        finally:
            await browser.close()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD, default today")
    ap.add_argument("--days", type=int, default=1,
                    help="window size ending on --date; 7 backfills a week")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-analyse leads already carrying a finding. Needed "
                         "after a mapping change, since the normal pass skips "
                         "anything with ConsentFinding set.")
    args = ap.parse_args()

    end = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else date.today()) + timedelta(days=1)
    start = end - timedelta(days=args.days)

    conn = crm_push.connect()
    leads = fetch_leads(conn, start, end, force=args.force)
    if not leads:
        print(f"no unanalysed leads created between {start} and {end}")
        return

    by_domain = defaultdict(list)
    skipped = []
    for lead in leads:
        domain = domain_of(lead["Email"])
        (by_domain[domain] if domain else skipped).append(lead)

    print(f"{len(leads)} lead(s) in {start}..{end - timedelta(days=1)}: "
          f"{len(by_domain)} distinct company domain(s), "
          f"{len(skipped)} without one")

    if args.dry_run:
        for d, ls in sorted(by_domain.items()):
            print(f"  would analyse {d:<32} ({len(ls)} lead(s))")
        for lead in skipped:
            print(f"  would skip     {str(lead['Email'])[:38]:<38} "
                  f"{reason_skipped(lead['Email'])[:50]}")
        print("\ndry run -- nothing written")
        return

    for lead in skipped:
        write_result(conn, [lead["Id"]], note=reason_skipped(lead["Email"]))
    if skipped:
        print(f"  recorded a reason on {len(skipped)} lead(s) with no company domain")

    results = asyncio.run(analyse(list(by_domain), not args.no_llm,
                                  args.concurrency))
    leads_done = gaps = clean = cmp_ = failed = 0
    for domain, data in results.items():
        ids = [l["Id"] for l in by_domain[domain]]
        if not data or data.get("error"):
            failed += 1
            write_result(conn, ids,
                         note=f"could not analyse {domain}: "
                              f"{(data or {}).get('error', 'no response')}")
            print(f"  FAIL  {domain:<34} {(data or {}).get('error','')[:46]}")
            continue
        write_result(conn, ids, data=data)
        leads_done += len(ids)
        score = data["consent_gap_score"]
        if data["has_cmp"]:
            cmp_ += 1
            print(f"  SKIP  {domain:<34} has a CMP: {data['cmp_name']}")
        elif score == 0:
            clean += 1
            print(f"  WEAK  {domain:<34} no gap")
        else:
            gaps += 1
            print(f"  OK    {domain:<34} gap {score}")

    print(f"\n{gaps} domain(s) with a consent gap, {clean} clean, "
          f"{cmp_} already have a CMP, {failed} unreachable")
    print(f"{leads_done} lead row(s) updated")
    conn.close()


if __name__ == "__main__":
    main()
