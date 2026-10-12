"""
ScrapeStatus / ReportStatus on the CRM mirror ae_companies. ae_companies is the source of
truth; this module says what each ae status means on the CRM, and audits the two.

set_status() writes CRM_VALUES[new_status] in the same step as the status change (see
status.py), so for a production company the CRM columns cannot lag. The audit exists for the
one window no code can close, a crash between the CRM write and the Postgres commit: it
finds disagreements, it never repairs them.
"""
from . import settings

SCRAPE = settings.CRM["scrape_status"]
REPORT = settings.CRM["report_status"]

# What the CRM is told when a company ENTERS a status. Statuses not listed write nothing:
# for example score_too_high and held_no_contact have no value in the existing vocabulary.
CRM_VALUES = {
    "scanned": {SCRAPE: "done"},
    "scan_failed": {SCRAPE: "failed"},
    "unscannable": {SCRAPE: "done", REPORT: "unscannable"},    # the old job paired them this way
    "no_gaps": {REPORT: "no_gap"},
    "reported": {REPORT: "generated"},
}

# Statuses past `reported` all imply a generated report; `scanned` onwards implies a done scrape.
_AFTER_REPORT = ("reported", "contact_ranked", "verified", "drafted", "written_to_crm", "sent", "expired")
_SCRAPED = ("scanned", "gate_passed", "score_too_high", *_AFTER_REPORT)


def expected(ae_status):
    """(ScrapeStatus, ReportStatus) the CRM should hold for a company at this status; None = no expectation."""
    if ae_status in _AFTER_REPORT:
        return ("done", "generated")
    if ae_status in CRM_VALUES:
        cols = CRM_VALUES[ae_status]
        if ae_status == "no_gaps":
            return ("done", cols[REPORT])
        return (cols.get(SCRAPE), cols.get(REPORT))
    if ae_status in _SCRAPED:
        return ("done", None)
    return (None, None)


def audit(reader):
    """
    Compares the CRM's two columns with what ae_companies implies, for every production company
    whose status implies something. Test and legacy companies are never audited.
    Returns [{company_id, status, expected, crm}], empty when everything agrees.
    """
    import db
    with db.get_cursor() as cur:
        cur.execute("""SELECT company_id, status FROM ae_companies
                       WHERE NOT is_test AND status <> 'legacy' ORDER BY company_id""")
        rows = [r for r in cur.fetchall() if expected(r["status"]) != (None, None)]
    held = reader.status_columns([r["company_id"] for r in rows])
    out = []
    for r in rows:
        want_scrape, want_report = expected(r["status"])
        got = held.get(r["company_id"], (None, None))
        if (want_scrape is not None and got[0] != want_scrape) or \
           (want_report is not None and got[1] != want_report):
            out.append({"company_id": r["company_id"], "status": r["status"],
                        "expected": (want_scrape, want_report), "crm": tuple(got)})
    return out
