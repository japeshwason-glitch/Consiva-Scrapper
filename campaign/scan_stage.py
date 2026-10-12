"""
Stage 4: gap analysis. For every `checked` company that has a domain and is due, run (or reuse)
a homepage scan and move it on:

    scan ok           -> scanned        CRM ScrapeStatus = 'done'
    scanner unscannable -> unscannable  CRM ScrapeStatus = 'done', ReportStatus = 'unscannable'
    scan failed       -> stays `checked` with next_retry_at (backoff), and after 2 retries
                         -> scan_failed CRM ScrapeStatus = 'failed'

A scan younger than settings.SCAN_REUSE_DAYS (same company, same domain) is reused instead of
repeated. Scans run outside any database transaction (they take minutes); each result is then
applied to its company in one short transaction, together with its CRM write, so a failed CRM write
undoes the status change and the company is retried (the stored scan is kept and reused).
Work per run is bounded (settings.SCANS_PER_RUN) and concurrency comes from settings.
"""
import asyncio

import psycopg2.extras

import db

from . import scan_adapter, settings, stages, status

DUE = "domain IS NOT NULL AND domain_source IS NOT NULL AND (next_retry_at IS NULL OR next_retry_at <= NOW())"


def _candidates(limit, only_test):
    where, params = f"status = 'checked' AND {DUE}", []
    if only_test is not None:
        where += " AND is_test = %s"
        params.append(only_test)
    scope, scope_params = status.scope_sql()
    where += scope
    params.extend(scope_params)
    with db.get_cursor() as cur:
        cur.execute(f"""SELECT company_id, domain, company_name, scan_attempts, is_test FROM ae_companies
                        WHERE {where} ORDER BY company_id LIMIT %s""", (*params, limit))
        return cur.fetchall()


def _apply(company, scan_id, seconds, outcome, error, category, counts):
    """One company's result, in one transaction. Raises if the CRM write (or anything) fails."""
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status FROM ae_companies WHERE company_id = %s FOR UPDATE",
                        (company["company_id"],))
            row = cur.fetchone()
            if row is None or row["status"] != "checked":
                return                                   # moved by someone else; nothing to do
            cur.execute("""UPDATE ae_companies SET scan_id = COALESCE(%s, scan_id), scan_seconds = %s
                           WHERE company_id = %s""", (scan_id, round(seconds, 2), company["company_id"]))
            cid = company["company_id"]
            if outcome == "ok":
                status.set_status(cur, cid, "scanned", "homepage scanned")
                counts["scanned"] += 1
            elif outcome == "unscannable":
                cur.execute("UPDATE ae_companies SET failure_category = %s WHERE company_id = %s", (category, cid))
                status.set_status(cur, cid, "unscannable", error or "homepage could not be read")
                counts["unscannable"] += 1
            else:
                result = stages.record_scan_failure(cur, cid, error, category)
                counts[result] += 1


async def _scan_all(todo, scanner, concurrency, counts):
    sem = asyncio.Semaphore(concurrency)

    async def one(company):
        async with sem:
            scan_id, seconds, outcome, error, category = None, 0.0, "failed", None, None
            reuse = await asyncio.to_thread(scan_adapter.reusable_scan, company["company_id"], company["domain"])
            if reuse:
                counts["reused"] += 1
                scan_id, seconds = reuse["id"], 0.0
                outcome = "ok" if reuse["scan_status"] == "ok" else "unscannable"
                error, category = reuse["unscannable_reason"], (
                    scan_adapter.unscannable_category(reuse["unscannable_reason"]) if outcome == "unscannable" else None)
            else:
                data, err, seconds = await scanner(company)
                counts["seconds"][company["company_id"]] = round(seconds, 2)
                row = await asyncio.to_thread(scan_adapter.scan_row_from, company, data, err,
                                              company["scan_attempts"] + 1)
                scan_id = await asyncio.to_thread(db.insert_report_scan, **row)
                if row["scan_status"] == "ok":
                    outcome = "ok"
                elif row["scan_status"] == "unscannable" and row.get("http_status") == 403:
                    # A 403 is the site refusing our scanner, not proof the site cannot be read: retry with
                    # backoff like any other failure; after the retries it is scan_failed.
                    error, category = row["unscannable_reason"], "blocked"
                    counts["failure_categories"][category] = counts["failure_categories"].get(category, 0) + 1
                elif row["scan_status"] == "unscannable":
                    outcome, error = "unscannable", row["unscannable_reason"]
                    category = scan_adapter.unscannable_category(error)
                else:
                    error = row.get("error") or err or "no data"
                    category = scan_adapter.classify_failure(error)
                    counts["failure_categories"][category] = counts["failure_categories"].get(category, 0) + 1
            try:
                await asyncio.to_thread(_apply, company, scan_id, seconds, outcome, error, category, counts)
            except Exception as exc:                     # a CRM write that failed, a refused value, ...
                counts["errors"] += 1
                counts["error_detail"].append(f"{company['company_id']}: {type(exc).__name__}: {str(exc)[:160]}")

    await asyncio.gather(*(one(c) for c in todo))


def run(limit=None, dry_run=False, only_test=None, scanner=None, concurrency=None):
    """Returns counts: candidates, reused, scanned, unscannable, retry, scan_failed, errors, seconds, failure_categories."""
    todo = _candidates(limit or settings.SCANS_PER_RUN, only_test)
    counts = {"candidates": len(todo), "reused": 0, "scanned": 0, "unscannable": 0, "retry": 0,
              "scan_failed": 0, "errors": 0, "error_detail": [], "seconds": {}, "failure_categories": {}}
    if dry_run:
        reusable = sum(1 for c in todo if scan_adapter.reusable_scan(c["company_id"], c["domain"]))
        return {"candidates": len(todo), "would_reuse": reusable, "would_scan": len(todo) - reusable}
    scanner = scanner or scan_adapter.run_scan
    asyncio.run(_scan_all(todo, scanner, concurrency or settings.SCAN_CONCURRENCY, counts))
    return counts
