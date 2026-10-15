"""
Intake for crm.CrmLeads (read-only): every lead that is not deleted and has an email becomes a production company at
`pending` with the lead itself as its one contact, then runs through the same stages as any other company.

Each lead gets a stable integer company id from ae_lead_map (ids above settings.LEAD_ID_BASE). The lead's website is
ScrapperWebsite; with none, stage 3 may still find the domain from a business email, else `no_website`.
Idempotent: the map, the company and the contact are all ON CONFLICT DO NOTHING.

    python -m campaign.leads [--dry-run]       prints the mapped company ids as IDS=1000000001,...
"""
import argparse

import db

from . import crm_read, intake, settings


def read_leads(conn):
    sql = ("SELECT Id, FirstName, Email, Company, JobTitle, ScrapperWebsite, Industry, CompanyScale "
           f"FROM {settings.CRM_LEADS_TABLE} WHERE IsDeleted = 0 AND Email IS NOT NULL AND LTRIM(RTRIM(Email)) <> '' "
           "AND AE_DraftStatus IS NULL "         # a lead that already carries a draft is done: never taken in again
           "ORDER BY CreatedAt, Id")
    crm_read.assert_read_only(sql)
    cur = conn.cursor()
    cur.execute(sql)
    return [{"lead_id": str(r[0]).lower(), "first_name": (r[1] or "").strip() or None, "email": r[2].strip(),
             "company_name": (r[3] or "").strip() or None, "job_title": (r[4] or "").strip() or None,
             "website": (r[5] or "").strip() or None, "industry": (r[6] or "").strip() or None,
             "company_scale": (r[7] or "").strip() or None} for r in cur.fetchall()]


def _pick(leads, n):
    """The first n leads, one per company domain (the website's, else a business email's): a small, varied sample."""
    seen, out = set(), []
    for lead in leads:
        domain = intake.domain_of(lead["website"]) or (
            lead["email"].rsplit("@", 1)[1].lower() if intake.is_business_email(lead["email"]) else None)
        if domain and domain not in seen:
            seen.add(domain)
            out.append(lead)
        if len(out) >= n:
            break
    return out


def run_intake(dry_run=False, limit=None):
    conn = crm_read.connect()
    try:
        leads = read_leads(conn)
    finally:
        conn.close()
    if limit:
        leads = _pick(leads, limit)
    out = {"leads": len(leads), "companies_new": 0, "ids": []}
    if dry_run:
        return out
    with db.get_connection() as c:
        with c.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
            for lead in leads:
                cur.execute("INSERT INTO ae_lead_map (lead_id) VALUES (%s) ON CONFLICT DO NOTHING", (lead["lead_id"],))
                cur.execute("SELECT company_id FROM ae_lead_map WHERE lead_id = %s", (lead["lead_id"],))
                cid = cur.fetchone()["company_id"]
                out["ids"].append(cid)
                cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, status_reason, website,
                                                         company_scale, company_name, industry)
                               VALUES (%s, FALSE, %s, 'pending', %s, %s, %s, %s, %s) ON CONFLICT (company_id) DO NOTHING
                               RETURNING company_id""",
                            (cid, intake.domain_of(lead["website"]), "taken in from crm.CrmLeads", lead["website"],
                             lead["company_scale"], lead["company_name"], lead["industry"]))
                if cur.fetchone():
                    out["companies_new"] += 1
                cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, job_title, first_name)
                               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (contact_id) DO NOTHING""",
                            (lead["lead_id"], cid, lead["email"], lead["job_title"], lead["first_name"]))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, help="take only this many leads, one per company domain (a sample)")
    a = ap.parse_args()
    r = run_intake(a.dry_run, a.limit)
    print(f"leads read: {r['leads']} | new companies: {r['companies_new']}")
    print("IDS=" + ",".join(map(str, r["ids"])))
