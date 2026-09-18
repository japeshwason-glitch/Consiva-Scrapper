"""
Consiva pipeline API.

    venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000

Three audiences, three groups of endpoints:

  /api/leads*      the CRM integration — pull undelivered leads, mark them taken
  /api/pipeline/*  operations — where today stands, run history, queue depth
  /api/companies*  the dashboard — browse and inspect what was found

Read-only over the pipeline's own state except for marking leads delivered.
Running the pipeline is the scheduler's job (run_daily.py under systemd), not
this service: a request-scoped background task dies with the process and has no
restart semantics, which is exactly what a machine that loses power needs.
"""

from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import fingerprints

app = FastAPI(
    title="Consiva pipeline",
    description="Lead pipeline state, CRM handoff and operational status.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

UI_DIR = Path(__file__).resolve().parent / "ui"

# A row whose domain was later rejected must never surface anywhere: those are
# companies we could not confidently identify, and they score HIGHEST of all,
# because a wrong domain is usually a big tracker-heavy site.
LIVE = "status NOT IN ('discovered', 'resolve_failed')"

LEAD_FIELDS = """
    cin, company_name, final_domain AS domain, business_scale,
    contact_name, contact_title, contact_email, contact_source,
    title_match_confidence, consent_gap_score, consent_gap_summary,
    company_tagline, reg_state, reg_pin, tracking_cookie_count,
    third_party_count, has_data_form, form_field_types,
    COALESCE(linkedin_industry, industrial_classification) AS industry
"""


class DeliveredRequest(BaseModel):
    cins: list[str]


# ---------------------------------------------------------------- CRM handoff

@app.get("/api/leads", tags=["crm"])
def leads(limit: int = Query(200, le=1000), include_delivered: bool = False):
    """
    Exported leads the CRM has not taken yet, oldest first. Poll this; every
    lead appears exactly once unless you ask for delivered ones too.
    """
    where = "status = 'exported'"
    if not include_delivered:
        where += " AND delivered_at IS NULL"
    with db.get_cursor() as cur:
        cur.execute(f"SELECT {LEAD_FIELDS} FROM companies WHERE {where} "
                    f"ORDER BY updated_at ASC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


@app.post("/api/leads/delivered", tags=["crm"])
def mark_delivered(body: DeliveredRequest):
    """
    Mark a batch as taken. Idempotent -- re-marking is a no-op, not an error,
    so a retry after a network failure is safe.
    """
    if not body.cins:
        return {"marked": 0}
    with db.get_cursor() as cur:
        cur.execute("""UPDATE companies SET delivered_at = NOW()
                       WHERE cin = ANY(%s) AND status = 'exported'
                         AND delivered_at IS NULL""", (body.cins,))
        return {"marked": cur.rowcount}


# ------------------------------------------------------------------ operations

@app.get("/api/pipeline/status", tags=["ops"])
def pipeline_status(target: int = 500):
    """
    Where today stands. `delivered_today` is COUNTED, never stored -- so it
    stays correct across a crash or a restart mid-run.
    """
    with db.get_cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM companies GROUP BY status")
        queue = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("""SELECT COUNT(*) AS n FROM companies
                       WHERE delivered_at::date = CURRENT_DATE""")
        today = cur.fetchone()["n"]
        cur.execute("""SELECT COUNT(*) AS n FROM companies
                       WHERE status='exported' AND delivered_at IS NULL""")
        waiting = cur.fetchone()["n"]
    return {
        "date": date.today().isoformat(),
        "delivered_today": today,
        "target": target,
        "remaining": max(0, target - today),
        "awaiting_delivery": waiting,
        "queue": queue,
    }


@app.get("/api/pipeline/runs", tags=["ops"])
def runs(limit: int = Query(50, le=500), stage: Optional[str] = None):
    """
    Recent stage executions. A row with finished_at NULL is a run that started
    and never completed -- which is what a power cut looks like from here.
    """
    where, params = "TRUE", []
    if stage:
        where, params = "stage = %s", [stage]
    params.append(limit)
    with db.get_cursor() as cur:
        cur.execute(f"""SELECT id, run_date, stage, started_at, finished_at,
                               exit_code, records_in, leads_after, note
                        FROM pipeline_runs WHERE {where}
                        ORDER BY started_at DESC LIMIT %s""", params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/pipeline/throughput", tags=["ops"])
def throughput(days: int = Query(14, le=90)):
    """Leads delivered per day, for trend and capacity planning."""
    with db.get_cursor() as cur:
        cur.execute("""SELECT delivered_at::date AS day, COUNT(*) AS leads
                       FROM companies
                       WHERE delivered_at >= CURRENT_DATE - %s::int
                       GROUP BY 1 ORDER BY 1 DESC""", (days,))
        return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------- dashboard

@app.get("/api/stats", tags=["dashboard"])
def stats():
    with db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM companies")
        total = cur.fetchone()["n"]
        cur.execute(f"""
            SELECT COUNT(*) FILTER (WHERE consent_gap_score IS NOT NULL) AS analysed,
                   COUNT(*) FILTER (WHERE {fingerprints.LEAD_SQL})      AS with_gap,
                   COUNT(*) FILTER (WHERE {fingerprints.SEVERE_SQL})    AS severe,
                   COUNT(*) FILTER (WHERE has_cmp)                      AS has_cmp,
                   COUNT(*) FILTER (WHERE has_data_form)                AS with_form,
                   COUNT(*) FILTER (WHERE has_data_form
                                      AND form_consent_checkbox)        AS form_consent,
                   COUNT(*) FILTER (WHERE contact_name IS NOT NULL)     AS with_contact
            FROM companies WHERE {LIVE}""")
        row = dict(cur.fetchone())
    row["total"] = total
    return row


@app.get("/api/companies", tags=["dashboard"])
def companies(q: str = "", max_score: int = fingerprints.ALIGNED,
              limit: int = Query(500, le=2000)):
    # 0 is the most severe gap and 100 is perfectly aligned, so "worth
    # calling" is an upper bound: max_score=50 means at least that much gap.
    where = [LIVE, "consent_gap_score IS NOT NULL", "consent_gap_score <= %s"]
    params = [max_score]
    if q:
        where.append("(company_name ILIKE %s OR resolved_domain ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    params.append(limit)
    with db.get_cursor() as cur:
        cur.execute(f"""SELECT cin, company_name, resolved_domain, final_domain,
                               consent_gap_score, business_scale, has_cmp, cmp_name,
                               status, tracking_cookie_count, third_party_count,
                               has_data_form, contact_name, contact_title,
                               industrial_classification, linkedin_employee_count
                        FROM companies WHERE {' AND '.join(where)}
                        ORDER BY {fingerprints.WORST_FIRST_SQL}, company_name ASC
                        LIMIT %s""", params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/companies/{cin}", tags=["dashboard"])
def company(cin: str):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE cin = %s", (cin,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "company not found")
    record = dict(row)
    for key in ("paidup_capital", "authorized_capital", "resolve_confidence"):
        if record.get(key) is not None:
            record[key] = float(record[key])
    for key in ("created_at", "updated_at", "delivered_at"):
        if record.get(key) is not None:
            record[key] = record[key].isoformat()
    return record


@app.get("/api/health", tags=["ops"])
def health():
    """Liveness for the load balancer / systemd watchdog."""
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return {"status": "ok", "database": "up"}
    except Exception as exc:
        raise HTTPException(503, f"database unreachable: {exc}")


if UI_DIR.exists():
    app.mount("/assets", StaticFiles(directory=UI_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(UI_DIR / "index.html")
