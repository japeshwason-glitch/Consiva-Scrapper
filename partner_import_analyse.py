"""
Consent-gap analysis for PARTNER-IMPORTED rows in dbo.CRM_Scrapper (job 3).

    python partner_import_analyse.py                  # up to today's quota
    python partner_import_analyse.py --max-minutes 90 # stop claiming after 90m
    python partner_import_analyse.py --status         # queue and today's count
    python partner_import_analyse.py --dry-run        # show the next chunk only

A partner inserts these rows directly (Source = 'PartnerImport'), with a
company, a website and usually a headcount band and industry -- but none of
the consent findings the MCA scraper supplies. This job crawls each Website
the same way stage 03 does and fills those findings in.

Fill-blanks only: every column is written as COALESCE(existing, ours), so
anything the partner (or sales) already put on the row wins. It is an UPDATE
job; it never inserts a row.

Progress lives on the row itself, in three columns this job adds:

    ScrapeStatus    NULL (queued) -> running -> done | retry | failed | no_website
    ScrapeAttempts  page-load failures so far; failed at MAX_ATTEMPTS
    ScrapedAt       last claim / finish time, UTC -- drives the daily quota

Rows are CLAIMED (set to 'running') in one atomic UPDATE before any page is
loaded, so two runs can never work the same lead. A claim older than
STALE_CLAIM_H is treated as a crashed run and becomes claimable again, so a
power cut costs at most one chunk of rework, never a stuck row.
"""
import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "stages"))
from importlib import import_module

import config
import crm_push
import fingerprints
import personas

_stage03 = import_module("03_scrape_playwright")

SOURCE = "PartnerImport"
DAILY_TARGET = int(os.environ.get("PARTNER_DAILY_TARGET", "500"))
CHUNK = 50                 # rows per claim: a crash reworks at most this many
CONCURRENCY = 6            # same page-load concurrency as the MCA scrape
MAX_ATTEMPTS = 3           # same as db.MAX_RETRIES for the MCA pipeline
STALE_CLAIM_H = 6          # a 'running' row this old belongs to a dead run
TABLE = config.CRM_DB_TABLE
# Touch this file (data/ is mounted from the host) to stop the job starting;
# delete it to resume. Checked once at start, so a run in flight is unaffected.
PAUSE_FILE = Path(__file__).resolve().parent / "data" / "partner.pause"

NUMERIC = ("GapScore", "TrackersPreConsent", "ThirdPartiesCount")


def ensure_columns(conn):
    """Add the three tracking columns if missing. Idempotent."""
    cur = conn.cursor()
    for col, ddl in (("ScrapeStatus", "NVARCHAR(20) NULL"),
                     ("ScrapeAttempts", "INT NOT NULL DEFAULT 0"),
                     ("ScrapedAt", "DATETIME2 NULL")):
        cur.execute(f"IF COL_LENGTH('{TABLE}', '{col}') IS NULL "
                    f"ALTER TABLE {TABLE} ADD {col} {ddl}")
    conn.commit()
    # Rows analysed before these columns existed are done, not queued.
    cur.execute(f"""
        UPDATE {TABLE} SET ScrapeStatus = 'done', ScrapedAt = UpdatedAt
        WHERE Source = %s AND ScrapeStatus IS NULL
          AND GapScore IS NOT NULL AND ConsentFinding IS NOT NULL""", (SOURCE,))
    conn.commit()


def today_start_utc():
    """Local midnight (the container runs on IST) as a naive UTC datetime."""
    local = datetime.now().astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def done_today(conn):
    """Sites finished today, success or terminal failure -- the quota counter."""
    cur = conn.cursor()
    cur.execute(f"""SELECT COUNT(*) FROM {TABLE}
                    WHERE Source = %s AND ScrapedAt >= %s
                      AND ScrapeStatus IN ('done', 'failed')""",
                (SOURCE, today_start_utc()))
    return cur.fetchone()[0]


# 'retry' waits out the same gap as a stale claim, so a site that is down for
# an hour is not burned through all its attempts inside a single run.
# A row with no Website is skipped outright, never claimed, written or counted:
# the partner bought that list in, and a guessed domain is not worth sending.
HAS_WEBSITE = "NULLIF(LTRIM(RTRIM(Website)), '') IS NOT NULL"

QUEUED = f"""Source = %s AND {HAS_WEBSITE} AND (ScrapeStatus IS NULL
             OR (ScrapeStatus IN ('running', 'retry')
                 AND ScrapedAt < DATEADD(hour, -{STALE_CLAIM_H}, SYSUTCDATETIME())))"""


# Unclaimed rows without a Website are reported as no_website, not queued.
QUEUE_LABEL = f"""CASE WHEN ScrapeStatus IS NOT NULL THEN ScrapeStatus
                      WHEN {HAS_WEBSITE} THEN 'queued' ELSE 'no_website' END"""


def status(conn):
    cur = conn.cursor()
    cur.execute(f"""SELECT {QUEUE_LABEL}, COUNT(*) FROM {TABLE}
                    WHERE Source = %s GROUP BY {QUEUE_LABEL}""", (SOURCE,))
    counts = dict(cur.fetchall())
    print(f"{SOURCE} rows in {config.CRM_DB_NAME}.{TABLE}: {counts or 'none'}")
    print(f"finished today: {done_today(conn)} / {DAILY_TARGET}")


def claim(conn, n):
    """Atomically mark up to n queued rows 'running' and return them."""
    cur = conn.cursor()
    cur.execute(f"""
        UPDATE TOP ({int(n)}) {TABLE} WITH (READPAST)
        SET ScrapeStatus = 'running', ScrapedAt = SYSUTCDATETIME()
        OUTPUT inserted.Id, inserted.CompanyName, inserted.Website,
               inserted.CompanyScale, inserted.Industry, inserted.ContactName,
               inserted.ScrapeAttempts
        WHERE {QUEUED}""", (SOURCE,))
    cols = ("Id", "CompanyName", "Website", "CompanyScale", "Industry",
            "ContactName", "ScrapeAttempts")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return rows


def peek(conn, n):
    cur = conn.cursor()
    cur.execute(f"SELECT TOP ({int(n)}) Id, CompanyName, Website FROM {TABLE} "
                f"WHERE {QUEUED} ORDER BY Id", (SOURCE,))
    return cur.fetchall()


def persona_scale(band):
    """
    The partner's headcount band ("501-1000", "5001 & above") as a persona
    tier. Only steers the target titles -- CompanyScale keeps the partner's
    own wording.
    """
    m = re.match(r"\s*(\d+)", str(band or ""))
    if not m:
        return None
    n = int(m.group(1))
    return "Enterprise" if n >= 500 else "SMB" if n > 50 else "MSME"


def domain_of(website):
    """Bare host from whatever the partner typed: www., scheme, path."""
    host = re.sub(r"^[a-z]+://", "", (website or "").strip().lower())
    host = host.split("/")[0].split("?")[0].rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host else None


def values_for(row, data):
    """{column: value} from one stage-03 analysis."""
    titles = [None, None, None]
    if not (row["ContactName"] or "").strip():
        # Same rule as crm_push: titles are a search instruction, so they are
        # left blank next to a contact who is already known.
        titles = (personas.predict(scale=persona_scale(row["CompanyScale"]),
                                   industry=row["Industry"], cin=None)
                  + titles)[:3]
    return {
        "Website":            data.get("final_domain"),
        "GapScore":           data.get("consent_gap_score"),
        "TrackersPreConsent": data.get("tracking_cookie_count"),
        "ThirdPartiesCount":  data.get("third_party_count"),
        "CookieBanner":       "present" if data.get("has_cookie_banner") else "none",
        "CollectsViaForm":    crm_push.form_sentence(data) or None,
        "ConsentFinding":     data.get("consent_gap_summary"),
        "TargetTitle1":       crm_push.clip(titles[0], 120),
        "TargetTitle2":       crm_push.clip(titles[1], 120),
        "TargetTitle3":       crm_push.clip(titles[2], 120),
    }


def finish(conn, row_id, state, values=None, attempts=None):
    """Write the findings (blanks only) and the row's final ScrapeStatus."""
    values = {k: v for k, v in (values or {}).items() if v is not None}
    sets = [f"{c} = COALESCE({c}, %s)" if c in NUMERIC
            else f"{c} = COALESCE(NULLIF({c}, ''), %s)" for c in values]
    params = list(values.values())
    sets.append("ScrapeStatus = %s")
    params.append(state)
    if attempts is not None:
        sets.append("ScrapeAttempts = %s")
        params.append(attempts)
    sets.append("ScrapedAt = SYSUTCDATETIME(), UpdatedAt = SYSUTCDATETIME()")
    cur = conn.cursor()
    cur.execute(f"UPDATE {TABLE} SET {', '.join(sets)} "
                f"WHERE Id = %s AND Source = %s", (*params, row_id, SOURCE))
    conn.commit()


async def analyse(browser, rows):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(row):
        async with sem:
            company = {"resolved_domain": domain_of(row["Website"]),
                       "company_name": row["CompanyName"],
                       "cin": f"partner:{row['Id']}"}
            try:
                # use_llm=False: the tagline and soft fields it would extract
                # have no column on this table.
                data, err = await _stage03.scrape_one(browser, company, False)
                return row, (data if data else {"error": err or "no data"})
            except Exception as exc:
                return row, {"error": f"{type(exc).__name__}: {exc}"[:200]}

    return await asyncio.gather(*(one(r) for r in rows))


def record(conn, row, data, tally):
    label = f"{row['Id']} {domain_of(row['Website']) or row['Website']!s:<30}"
    if data.get("error"):
        attempts = (row["ScrapeAttempts"] or 0) + 1
        if attempts >= MAX_ATTEMPTS:
            finish(conn, row["Id"], "failed", attempts=attempts, values={
                "ConsentFinding": f"could not analyse {domain_of(row['Website'])}: "
                                  f"{data['error']}"[:1000]})
            tally["failed"] += 1
            print(f"  FAIL  {label} {data['error'][:60]}", flush=True)
        else:
            # Claimable again after STALE_CLAIM_H; not counted toward the quota.
            finish(conn, row["Id"], "retry", attempts=attempts)
            tally["retry"] += 1
            print(f"  RETRY {label} attempt {attempts}: {data['error'][:50]}",
                  flush=True)
        return
    finish(conn, row["Id"], "done", values=values_for(row, data))
    score = data["consent_gap_score"]
    if data["has_cmp"]:
        tally["cmp"] += 1
        verdict = f"has a CMP: {data['cmp_name']}"
    elif fingerprints.is_lead_score(score):
        tally["gap"] += 1
        verdict = f"gap {score}"
    else:
        tally["clean"] += 1
        verdict = "no gap"
    print(f"  OK    {label} {verdict}", flush=True)


async def run(conn, quota, deadline):
    from playwright.async_api import async_playwright
    tally = dict(gap=0, clean=0, cmp=0, failed=0, retry=0, no_website=0)
    left = quota
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            while left > 0 and time.monotonic() < deadline:
                rows = claim(conn, min(CHUNK, left))
                if not rows:
                    break
                usable = []
                for row in rows:
                    if domain_of(row["Website"]):
                        usable.append(row)
                    else:
                        finish(conn, row["Id"], "no_website")
                        tally["no_website"] += 1
                for row, data in await analyse(browser, usable):
                    record(conn, row, data, tally)
                left = quota - done_today(conn)
        finally:
            await browser.close()
    return tally


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-minutes", type=float, default=None,
                    help="stop claiming new chunks after this long; the chunk "
                         "in flight still finishes")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if PAUSE_FILE.exists() and not args.status:
        print(f"partner import paused ({PAUSE_FILE.name} present) -- delete it to resume")
        return

    conn = crm_push.connect()
    ensure_columns(conn)
    if args.status:
        status(conn)
        return

    quota = DAILY_TARGET - done_today(conn)
    print(f"partner import: {done_today(conn)}/{DAILY_TARGET} done today, "
          f"{max(quota, 0)} left in today's quota", flush=True)
    if quota <= 0:
        return
    if args.dry_run:
        for r in peek(conn, min(CHUNK, quota)):
            print(f"  would analyse {r[0]} {r[1]!s:<40} {r[2]}")
        print("dry run -- nothing claimed or written")
        return

    deadline = time.monotonic() + (args.max_minutes * 60 if args.max_minutes
                                   else float("inf"))
    tally = asyncio.run(run(conn, quota, deadline))
    print(f"\n{tally['gap']} with a consent gap, {tally['clean']} clean, "
          f"{tally['cmp']} already have a CMP, {tally['failed']} unreachable, "
          f"{tally['retry']} queued for retry, {tally['no_website']} without a website")
    status(conn)
    conn.close()


if __name__ == "__main__":
    main()
