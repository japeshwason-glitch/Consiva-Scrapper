"""
Stage 6: the report. Only `gate_passed` companies get one: the gate runs before anything is
rendered (the old code never coupled the two; render_fitted() takes a stored scan).

For each: render the one-page PDF with the existing code (report_build.render_fitted), upload it to
R2 (storage.upload_report), write the `reports` row (db.insert_report), store report_ref, report_url
and report_date on ae_companies, and move to `reported` with the CRM's DownloadReportUrl,
ReportGeneratedAt and ReportStatus = 'generated' written in the same step.

A report already rendered for the same scan, template and catalogue version is reused, not rebuilt.
A rescan produces a new scan, hence a new report, whose URL overwrites the CRM's; the older report row
becomes `superseded` (db.insert_report) and its PDF stays in R2: nothing here ever deletes from R2.
Test companies get the full treatment (render, R2 upload, reports row) but no CRM write.
A report that cannot be rendered to one page is recorded as failed; the company stays `gate_passed`.
"""
import asyncio
import hashlib

import psycopg2.extras

import db
import report_findings as rf

from . import crm_write, settings, status

PER_RUN_DEFAULT = 60


def _candidates(limit, only_test):
    where, params = "status = 'gate_passed'", []
    if only_test is not None:
        where += " AND is_test = %s"
        params.append(only_test)
    scope, scope_params = status.scope_sql()
    where += scope
    params.extend(scope_params)
    with db.get_cursor() as cur:
        cur.execute(f"""SELECT company_id, domain, scan_id, is_test FROM ae_companies
                        WHERE {where} ORDER BY company_id LIMIT %s""", (*params, limit))
        return cur.fetchall()


def _scan(scan_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM report_scans WHERE id = %s", (scan_id,))
        return cur.fetchone()


def _existing(scan_id, template_version):
    with db.get_cursor() as cur:
        cur.execute("""SELECT id, public_url FROM reports WHERE scan_id = %s AND status = 'rendered'
                       AND template_version = %s AND catalogue_version = %s
                       ORDER BY generated_at DESC LIMIT 1""", (scan_id, template_version, rf.CATALOGUE_VERSION))
        return cur.fetchone()


def _finish(company, scan, report_id, url, counts):
    """The company's transition, in one transaction with its CRM write."""
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status FROM ae_companies WHERE company_id = %s FOR UPDATE", (company["company_id"],))
            row = cur.fetchone()
            if row is None or row["status"] != "gate_passed":
                return
            cur.execute("""UPDATE ae_companies SET report_ref = %s, report_url = %s, report_date = %s
                           WHERE company_id = %s""", (report_id, url, scan["scanned_at"], company["company_id"]))
            status.set_status(cur, company["company_id"], "reported", "report generated",
                              crm={"DownloadReportUrl": url, "ReportGeneratedAt": crm_write.SERVER_NOW})
    if not company["is_test"]:
        db.mark_report_pushed(report_id)
    counts["reported"] += 1


async def _default_renderer_factory():
    """(renderer, closer): one Chromium for the whole run, renders limited to two at a time."""
    import report_build as rb
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    sem = asyncio.Semaphore(2)

    async def renderer(domain, scan_date, scan):
        async with sem:
            try:
                return await asyncio.wait_for(rb.render_fitted(browser, domain, scan_date, scan),
                                              settings.RENDER_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None, f"render did not finish within {settings.RENDER_TIMEOUT_S}s", None

    async def close():
        await browser.close()
        await pw.stop()
    return renderer, close


async def _run_all(todo, renderer, uploader, counts):
    import report_build as rb
    closer = None
    if renderer is None:
        renderer, closer = await _default_renderer_factory()
    if uploader is None:
        import storage
        uploader = storage.upload_report
    try:
        for company in todo:                      # sequential: renders are the slow, memory-heavy part
            cid = company["company_id"]
            try:
                scan = await asyncio.to_thread(_scan, company["scan_id"])
                if scan is None:
                    raise ValueError("stored scan not found")
                found = await asyncio.to_thread(_existing, scan["id"], rb.TEMPLATE_VERSION)
                if found:
                    counts["reused"] += 1
                    report_id, url = found["id"], found["public_url"]
                else:
                    domain = scan["final_domain"] or company["domain"]
                    pdf, why, fit = await renderer(domain, rb.scan_date_text(scan["scanned_at"]), scan)
                    if not pdf:
                        counts["render_failed"] += 1
                        await asyncio.to_thread(db.insert_report, scan_id=scan["id"], lead_ref=cid, status="failed",
                                                template_version=rb.TEMPLATE_VERSION,
                                                catalogue_version=rf.CATALOGUE_VERSION, error=why)
                        continue
                    key, url = await asyncio.to_thread(uploader, pdf, domain)
                    report_id = await asyncio.to_thread(
                        db.insert_report, scan_id=scan["id"], lead_ref=cid, status="rendered",
                        template_version=rb.TEMPLATE_VERSION, catalogue_version=rf.CATALOGUE_VERSION,
                        object_key=key, public_url=url, pdf_sha256=hashlib.sha256(pdf).hexdigest(),
                        size_bytes=len(pdf))
                    counts["urls"][cid] = url
                await asyncio.to_thread(_finish, company, scan, report_id, url, counts)
            except Exception as exc:
                counts["errors"] += 1
                counts["error_detail"].append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        if closer:
            await closer()


def run(limit=None, dry_run=False, only_test=None, renderer=None, uploader=None):
    """Counts: candidates, reported, reused, render_failed, errors, urls (dry run: would_report)."""
    todo = _candidates(limit or PER_RUN_DEFAULT, only_test)
    if dry_run:
        return {"candidates": len(todo), "would_report": len(todo)}
    counts = {"candidates": len(todo), "reported": 0, "reused": 0, "render_failed": 0, "errors": 0,
              "error_detail": [], "urls": {}}
    if todo:
        asyncio.run(_run_all(todo, renderer, uploader, counts))
    return counts
