"""
Read-only JSON API over the pipeline database, for the dashboard UI.

    venv/bin/uvicorn api:app --reload --port 8000

Read-only by design: the dashboard shows what the pipeline found, it does not
change pipeline state. All database access goes through db.py, same as the
stage scripts.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import db

app = FastAPI(title="Consiva pipeline API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

UI_DIR = Path(__file__).parent / "ui"

# A row whose domain was later rejected must not appear anywhere in the
# dashboard: those are companies we could not confidently identify, and several
# of them (marvel.com for MARVEL KING LIMITED) score highest of all precisely
# because a wrong domain is usually a big tracker-heavy site.
LIVE = "status NOT IN ('discovered', 'resolve_failed')"

LIST_COLUMNS = """
    cin, company_name, resolved_domain, final_domain, consent_gap_score,
    business_scale, has_cmp, cmp_name, status, tracking_cookie_count,
    third_party_count, has_data_form, contact_name, contact_title,
    industrial_classification, linkedin_employee_count
"""


@app.get("/api/stats")
def stats():
    """Headline numbers for the dashboard summary bar."""
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM companies")
        total = cur.fetchone()["n"]
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE consent_gap_score IS NOT NULL) AS analysed,
                   COUNT(*) FILTER (WHERE consent_gap_score > 0)        AS with_gap,
                   COUNT(*) FILTER (WHERE consent_gap_score >= 50)      AS severe,
                   COUNT(*) FILTER (WHERE has_cmp)                      AS has_cmp,
                   COUNT(*) FILTER (WHERE has_data_form)                AS with_form,
                   COUNT(*) FILTER (WHERE has_data_form
                                      AND form_consent_checkbox)        AS form_consent,
                   COUNT(*) FILTER (WHERE contact_name IS NOT NULL)     AS with_contact
            FROM companies WHERE {LIVE}""")
        row = dict(cur.fetchone())
    row["total"] = total
    return row


@app.get("/api/companies")
def companies(q: str = "", min_score: int = 0, limit: int = 500):
    """The list pane: one row per analysed company, worst gap first."""
    where = [LIVE, "consent_gap_score IS NOT NULL", "consent_gap_score >= %s"]
    params = [min_score]
    if q:
        where.append("(company_name ILIKE %s OR resolved_domain ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    params.append(limit)

    with db.get_cursor() as cur:
        cur.execute(f"""SELECT {LIST_COLUMNS} FROM companies
                        WHERE {' AND '.join(where)}
                        ORDER BY consent_gap_score DESC, company_name ASC
                        LIMIT %s""", params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/companies/{cin}")
def company(cin: str):
    """Everything the pipeline recorded about one company."""
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE cin = %s", (cin,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "company not found")
    record = dict(row)
    for key in ("paidup_capital", "authorized_capital", "resolve_confidence"):
        if record.get(key) is not None:
            record[key] = float(record[key])
    for key in ("created_at", "updated_at"):
        if record.get(key) is not None:
            record[key] = record[key].isoformat()
    return record


@app.get("/api/leads")
def leads(limit: int = 200, include_delivered: bool = False):
    """
    The CRM handoff. Returns exported leads that have not been delivered yet,
    oldest first, so an integration can poll this endpoint and never receive
    the same lead twice.
    """
    where = "status = 'exported'"
    if not include_delivered:
        where += " AND delivered_at IS NULL"
    with db.get_cursor() as cur:
        cur.execute(f"""
            SELECT cin, company_name, final_domain AS domain, business_scale,
                   contact_name, contact_title, contact_email, contact_source,
                   title_match_confidence, consent_gap_score, consent_gap_summary,
                   company_tagline, reg_state, reg_pin,
                   COALESCE(linkedin_industry, industrial_classification) AS industry
            FROM companies WHERE {where}
            ORDER BY updated_at ASC LIMIT %s""", (limit,))
        return [dict(r) for r in cur.fetchall()]


@app.post("/api/leads/delivered")
def mark_delivered(cins: list[str]):
    """
    Called by the CRM once it has stored a batch. Idempotent: re-marking an
    already-delivered lead is a no-op rather than an error.
    """
    if not cins:
        return {"marked": 0}
    with db.get_cursor() as cur:
        cur.execute("""UPDATE companies SET delivered_at = NOW()
                       WHERE cin = ANY(%s) AND status='exported'
                         AND delivered_at IS NULL""", (cins,))
        return {"marked": cur.rowcount}


if UI_DIR.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIR), name="assets")

    @app.get("/")
    def index():
        return FileResponse(UI_DIR / "index.html")
