"""
Stage 5: the score gate.

For each `scanned` company, from its stored scan (report_findings.evaluate over the scan row):

    zero gaps                      -> no_gaps         CRM ReportStatus = 'no_gap'
    score above 90 (config)        -> score_too_high
    score 0-90 and >= 1 gap        -> gate_passed     band A 0-24, B 25-50, C 51-74, D 75-90

Zero gaps is tested first, so a clean site (score 100) and a site whose only finding is an
advisory (score 75, one review point) are both no_gaps. The score, gap count, band and scan date
go on ae_companies. The CRM gets the six consent columns and UpdatedAt that the retired report job
wrote for a completed scan (the values stored with the scan, built by report_build.crm_values), and
for no_gaps also ReportGeneratedAt and a NULL DownloadReportUrl, exactly like the legacy no_gap rows.
Test companies write nothing to the CRM.
"""
from collections import Counter

import psycopg2.extras

import db
import report_findings as rf

from . import bands, crm_write, settings, status


def decide(score, gap_count):
    """(next status, band). Zero gaps first, then the score ceiling, then the band."""
    if gap_count < settings.MIN_GAPS:
        return "no_gaps", None
    if score > settings.SKIP_ABOVE_SCORE:
        return "score_too_high", None
    return "gate_passed", bands.band_for(score)


def _scan(scan_id):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM report_scans WHERE id = %s", (scan_id,))
        return cur.fetchone()


def _ensure_findings(scan_id, findings):
    """The findings of a scan are stored once, whichever stage sees it first."""
    with db.get_cursor() as cur:
        cur.execute("SELECT 1 FROM report_findings WHERE scan_id = %s LIMIT 1", (scan_id,))
        if cur.fetchone():
            return
    db.insert_report_findings(scan_id, findings, rf.CATALOGUE_VERSION)


def run(limit=None, dry_run=False, only_test=None):
    """Counts: claimed, decisions, bands, errors (dry run: would)."""
    counts = {"claimed": 0, "decisions": {}, "bands": {}, "errors": 0, "error_detail": [], "would": {}}
    decisions, band_count = Counter(), Counter()
    with status.claim("scanned", limit or 1000, is_test=only_test) as (cur, rows):
        counts["claimed"] = len(rows)
        for company in rows:
            cid = company["company_id"]
            try:
                scan = _scan(company["scan_id"]) if company["scan_id"] else None
                if scan is None or scan["scan_status"] != "ok" or not scan["crm_values"]:
                    raise ValueError("no usable stored scan")
                result = rf.evaluate(scan)
                score, gap_count = result["score"], result["gap_count"]
                if score != scan["crm_values"]["GapScore"]:
                    raise ValueError(f"report score {score} != CRM GapScore {scan['crm_values']['GapScore']}")
                decision, band = decide(score, gap_count)
                if dry_run:
                    counts["would"][decision] = counts["would"].get(decision, 0) + 1
                    continue
                cols = {**scan["crm_values"], "UpdatedAt": crm_write.SERVER_NOW}
                if decision == "no_gaps":
                    cols.update(DownloadReportUrl=None, ReportGeneratedAt=crm_write.SERVER_NOW)
                _ensure_findings(scan["id"], result["findings"])
                with status.step(cur):
                    cur.execute("""UPDATE ae_companies SET score = %s, gap_count = %s, band = %s,
                                   report_date = %s WHERE company_id = %s""",
                                (score, gap_count, band, scan["scanned_at"], cid))
                    status.set_status(cur, cid, decision,
                                      f"score {score}, {gap_count} gap(s)" + (f", band {band}" if band else ""),
                                      crm=cols)
                decisions[decision] += 1
                if band:
                    band_count[band] += 1
            except Exception as exc:        # a refused value, a CRM write that failed, a missing scan
                counts["errors"] += 1
                counts["error_detail"].append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
    counts["decisions"], counts["bands"] = dict(decisions), dict(band_count)
    return counts
