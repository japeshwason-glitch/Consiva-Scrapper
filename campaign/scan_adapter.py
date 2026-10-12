"""
Stage 4 adapter: one scan for ONE company, by calling the existing scanner's internal
functions directly. Nothing in the old scanner changes.

  * the scan itself is report_build._scan_worker -> stages/03_scrape_playwright.scrape_one(
    report_mode=True), run in a child Python process under a hard timeout so a frozen page
    cannot stall a run (the same reason the old job isolated it). The child is started with
    `python -c`, never as `report_build.py` (that file refuses to run as a script now);
  * the keying is unchanged: report_scans.lead_ref = company_id, cin = "partner:<company_id>";
  * the stored row is report_build.scan_row() plus the six CRM values from
    report_build.crm_values() (the old formatting functions) and the readiness score, which must
    equal the CRM GapScore for the same scan or the scan counts as failed.
"""
import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import db

from . import settings

ROOT = Path(__file__).resolve().parent.parent
FAILURE_CATEGORIES = ("dns", "timeout", "blocked", "http_error", "other")

WORKER_CODE = ("import asyncio, json, sys, report_build as rb; "
               "asyncio.run(rb._scan_worker(json.loads(sys.argv[1])))")


class ScanIdentityError(Exception):
    pass


def classify_failure(error):
    """dns / timeout / blocked / http_error / other, from the text of a scan failure."""
    e = (error or "").lower()
    if any(k in e for k in ("err_name_not_resolved", "name or service not known", "getaddrinfo",
                            "enotfound", "nxdomain", "dns")):
        return "dns"
    if any(k in e for k in ("timeout", "timed out", "timed_out", "did not finish")):
        return "timeout"
    if any(k in e for k in ("captcha", "challenge", "blocked", "cloudflare", "bot check", " 403")):
        return "blocked"
    if re.search(r"\bhttp (4|5)\d\d\b|status (4|5)\d\d", e) or "err_http_response_code_failure" in e:
        return "http_error"
    return "other"


def unscannable_category(reason):
    """A category for the scanner's own `unscannable_reason` text."""
    r = (reason or "").lower()
    if "bot check" in r or "challenge" in r:
        return "blocked"
    if "http" in r:
        return "http_error"
    return "other"


def payload(company):
    """What the scanner is told about one company (the existing keying)."""
    return {"resolved_domain": company["domain"], "company_name": company.get("company_name") or "",
            "cin": f"partner:{company['company_id']}"}


def scan_argv(company):
    return [sys.executable, "-W", "ignore", "-c", WORKER_CODE, json.dumps(payload(company))]


def _kill_group(pid):
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        time.sleep(grace)


async def run_scan(company, timeout=None, argv=None):
    """(data | None, error | None, seconds). One attempt, in a killable child process."""
    timeout = timeout or settings.SCAN_TIMEOUT_S
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *(argv or scan_argv(company)), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, cwd=str(ROOT))
    try:
        out, errout = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        await asyncio.to_thread(_kill_group, proc.pid)
        await proc.wait()
        return None, f"homepage did not finish within {timeout}s", time.monotonic() - started
    seconds = time.monotonic() - started
    try:
        result = json.loads(out.decode().strip().splitlines()[-1])
    except (IndexError, ValueError):
        tail = errout.decode(errors="replace").strip().splitlines()[-1:] or [""]
        return None, f"scan worker exited {proc.returncode}: {tail[0][:160]}", seconds
    if result.get("data"):
        return result["data"], None, seconds
    return None, result.get("error") or "no data", seconds


def scan_row_from(company, data, err, attempts=1):
    """
    The report_scans row for one scan. For a completed scan it carries the six CRM values and
    the readiness score; if the score the report would print differs from the GapScore the CRM
    would hold for the same scan, the row is stored as a failed scan instead.
    """
    import report_build as rb
    import report_findings as rf
    lead = {"Id": company["company_id"], "CompanyName": company.get("company_name")}
    row = rb.scan_row(lead, company["domain"], data, err, attempts)
    if data and row["scan_status"] == "ok":
        values = rb.crm_values(data)
        row["crm_values"] = values
        row["readiness_score"] = rf.evaluate(row)["score"]
        if row["readiness_score"] != values["GapScore"]:
            msg = (f"report score {row['readiness_score']} != CRM GapScore {values['GapScore']} "
                   f"for the same scan")
            return {**{k: row[k] for k in ("lead_ref", "company_name", "requested_domain", "scanner_sha",
                                          "fingerprints_version", "attempts")},
                    "scan_status": "failed", "error": msg}
    return row


def reusable_scan(company_id, domain, days=None):
    """The newest stored scan of this company and domain younger than `days`, or None."""
    days = settings.SCAN_REUSE_DAYS if days is None else days
    with db.get_cursor() as cur:
        cur.execute("""SELECT * FROM report_scans
                       WHERE lead_ref = %s AND requested_domain = %s
                         AND scanned_at >= NOW() - make_interval(days => %s)
                         AND ((scan_status = 'unscannable' AND http_status IS DISTINCT FROM 403)
                              OR (scan_status = 'ok' AND crm_values IS NOT NULL))
                       ORDER BY scanned_at DESC LIMIT 1""", (company_id, domain, days))
        return cur.fetchone()
