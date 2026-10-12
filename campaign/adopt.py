"""
Adopting a company that ALREADY has a generated report in the CRM (a legacy lead): the existing report and its
CRM report columns are left exactly as they are. The company is taken in as a production company, its contacts
go through the free checks and the domain stage, and it is placed at `reported` with the report's URL, score and
date from the CRM, so contact ranking, NeverBounce and drafting can continue. Nothing about the report is
written to the CRM (set_status runs with write_crm=False). A fresh homepage scan is stored only to give the draft its
structured findings (vendors, tracker counts, domains); it creates no PDF and changes no CRM column.

Refused: a company that is not a PartnerImport row with ReportStatus 'generated', a report URL off the report
host, a report older than the freshness window, a company already in ae_companies, or one with no gap in the fresh
scan or a CRM score above the ceiling (nothing to write about).
"""
import asyncio

import psycopg2.extras

import db
import report_findings as rf

from . import bands, checks, crm_write, domains, intake, scan_adapter, settings, status


class AdoptRefused(Exception):
    pass


def _check_row(row):
    if row["report_status"] != "generated" or not row["report_url"]:
        raise AdoptRefused(f"company {row['id']}: no generated report in the CRM (ReportStatus={row['report_status']!r})")
    if not crm_write.report_url_re().match(row["report_url"]):
        raise AdoptRefused(f"company {row['id']}: the report URL is not on the report host")
    if row["gap_score"] is None or not (0 <= row["gap_score"] <= settings.SKIP_ABOVE_SCORE):
        raise AdoptRefused(f"company {row['id']}: CRM score {row['gap_score']} is outside 0-{settings.SKIP_ABOVE_SCORE}")


def adopt(reader, ids, scanner=None, resolver=None):
    """Adopts each id; returns [{company_id, status, ...}] and raises AdoptRefused for a bad row before touching anything."""
    ids = list(ids)
    rows = {r["id"]: r for r in reader.report_rows(ids)}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise AdoptRefused(f"not PartnerImport companies in the CRM: {missing}")
    for i in ids:
        _check_row(rows[i])
    with db.get_cursor() as cur:
        cur.execute("SELECT company_id FROM ae_companies WHERE company_id = ANY(%s)", (ids,))
        have = [r["company_id"] for r in cur.fetchall()]
    if have:
        raise AdoptRefused(f"already in ae_companies: {have}")
    fresh = [r["report_generated"] for r in rows.values()
             if r["report_generated"] is None]
    if fresh:
        raise AdoptRefused("a report has no ReportGeneratedAt date")

    # 1. take in as production companies with their contacts; legacy rule and the already-emailed hold are
    #    deliberately not applied: the caller named these companies.
    contacts = reader.contacts(ids)
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for i in ids:
                r = rows[i]
                cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, status_reason, website,
                                   company_scale, company_name, industry)
                               VALUES (%s, FALSE, %s, 'pending', 'adopted: the CRM already holds its report', %s, %s, %s, %s)""",
                            (i, intake.domain_of(r["website"]), r["website"] or None, r["company_scale"],
                             r["company_name"], r["industry"] or None))
            intake._insert_contacts(cur, contacts, {"contacts_new": 0, "contacts_business_email": 0})
    # 2. the same free checks and domain stage as any company (they only see these pending companies)
    with status.only_companies(ids):
        checks.run(only_test=False, resolver=resolver)
        domains.run(only_test=False)
    # 3. a fresh scan, stored for its structured findings only
    out = []
    scanner = scanner or scan_adapter.run_scan
    for i in ids:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_companies WHERE company_id = %s", (i,))
            company = cur.fetchone()
        if company["status"] != "checked" or not company["domain"]:
            out.append({"company_id": i, "status": company["status"], "note": company["status_reason"]})
            continue
        data, err, secs = asyncio.run(scanner({"company_id": i, "company_name": company["company_name"],
                                               "domain": company["domain"]}))
        row = scan_adapter.scan_row_from({"company_id": i, "company_name": company["company_name"],
                                          "domain": company["domain"]}, data, err, 1)
        scan_id = db.insert_report_scan(**row)
        if row["scan_status"] != "ok" or rf.evaluate(row)["gap_count"] < settings.MIN_GAPS:
            out.append({"company_id": i, "status": "checked",
                        "note": f"not adopted: scan {row['scan_status']}, no gap to write about"})
            continue
        r, result = rows[i], rf.evaluate(row)
        band = bands.band_for(r["gap_score"])
        with db.get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""UPDATE ae_companies SET scan_id = %s, scan_seconds = %s, score = %s, band = %s, gap_count = %s,
                               report_url = %s, report_date = %s WHERE company_id = %s""",
                            (scan_id, round(secs, 2), r["gap_score"], band, result["gap_count"], r["report_url"],
                             r["report_generated"], i))
                for nxt in ("scanned", "gate_passed", "reported"):
                    status.set_status(cur, i, nxt, "adopted: existing CRM report kept as it is", write_crm=False)
        out.append({"company_id": i, "status": "reported", "score": r["gap_score"], "band": band,
                    "gaps": result["gap_count"], "report_url": r["report_url"]})
    return out
