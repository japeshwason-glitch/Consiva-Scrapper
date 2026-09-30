"""
Consiva pipeline API.

    venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000

Three audiences, three groups of endpoints:

  /api/leads*      the CRM integration — pull undelivered leads, mark them taken
  /api/pipeline/*  operations — where today stands, run history, queue depth
  /api/companies*  the dashboard — browse and inspect what was found
  /api/partner/*   partner-imported rows in the CRM table (job 3) — progress

Read-only over the pipeline's own state except for marking leads delivered.
Running the pipeline is the scheduler's job (run_daily.py under systemd), not
this service: a request-scoped background task dies with the process and has no
restart semantics, which is exactly what a machine that loses power needs.
"""

import faulthandler
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import fingerprints
import partner_import_analyse as pia

# `kill -USR1 <worker pid>` prints every thread's stack to the container log.
# Added after the partner endpoints wedged with every thread parked on a lock
# and nothing to say which one.
faulthandler.register(signal.SIGUSR1, all_threads=True)

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


# ------------------------------------------------------------ partner import

# These rows live in the sales team's SQL Server, and requests never touch it.
# Querying it per request twice wedged every worker thread while the CRM link
# was flaky. A refresher runs partner_snapshot.py as a separate process under
# a hard timeout and the endpoints read the file it writes, so a hung CRM
# means stale numbers on the page, never a frozen API.
SNAPSHOT_PATH = Path("/tmp/partner_snapshot.json")
SNAPSHOT_LOCK = Path("/tmp/partner_snapshot.lock")
SNAPSHOT_EVERY_S = 60
SNAPSHOT_TIMEOUT_S = 90
STALE_AFTER_S = 5 * 60

_snap_cache = {"mtime": None, "data": None}


def _refresh_snapshot():
    """One refresh, by whichever worker holds the lock; the rest skip."""
    import fcntl
    with open(SNAPSHOT_LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            age = time.time() - SNAPSHOT_PATH.stat().st_mtime
        except FileNotFoundError:
            age = None
        if age is not None and age < SNAPSHOT_EVERY_S - 15:
            return          # another worker refreshed it moments ago
        try:
            subprocess.run([sys.executable, str(Path(__file__).parent / "partner_snapshot.py"),
                            str(SNAPSHOT_PATH)],
                           timeout=SNAPSHOT_TIMEOUT_S, capture_output=True)
        except subprocess.TimeoutExpired:
            print(f"partner snapshot timed out after {SNAPSHOT_TIMEOUT_S}s "
                  f"-- serving the previous one", flush=True)


def _snapshot_loop():
    while True:
        try:
            _refresh_snapshot()
        except Exception as exc:
            print(f"partner snapshot refresh failed: {exc}", flush=True)
        time.sleep(SNAPSHOT_EVERY_S)


threading.Thread(target=_snapshot_loop, daemon=True, name="partner-snapshot").start()


def _snapshot():
    try:
        mtime = SNAPSHOT_PATH.stat().st_mtime
    except FileNotFoundError:
        raise HTTPException(503, "partner data is still loading -- try again in a minute")
    if _snap_cache["mtime"] != mtime:
        _snap_cache["data"] = json.loads(SNAPSHOT_PATH.read_text())
        _snap_cache["mtime"] = mtime
    return _snap_cache["data"]


def _utc(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


@app.get("/api/partner/status", tags=["partner"])
def partner_status(days: int = Query(14, le=90)):
    """Queue by ScrapeStatus, today's quota, verdicts, and per-day throughput."""
    snap = _snapshot()
    rows = snap["rows"]
    now = datetime.now(timezone.utc)
    today = pia.today_start_utc().replace(tzinfo=timezone.utc)
    ist = timedelta(minutes=330)        # the quota counts IST days

    queue, per_day = {}, {}
    verdicts = {"severe_gap": 0, "gap": 0, "clean": 0, "has_cmp": 0}
    done_today = in_flight = 0
    touched_today = []
    for r in rows:
        queue[r["status"]] = queue.get(r["status"], 0) + 1
        at = _utc(r["scraped_at"])
        finished = r["status"] in ("done", "failed")
        if at and at >= today:
            touched_today.append(at)
            done_today += finished
        if finished and at and at >= now - timedelta(days=days):
            day = (at + ist).date().isoformat()
            per_day[day] = per_day.get(day, 0) + 1
        if r["status"] == "running" and at and at >= now - timedelta(minutes=30):
            in_flight += 1
        if r["status"] == "done" and r["gap_score"] is not None:
            if (r["finding"] or "").startswith("CMP detected"):
                verdicts["has_cmp"] += 1
            elif r["gap_score"] <= fingerprints.SEVERE_AT:
                verdicts["severe_gap"] += 1
            elif r["gap_score"] < fingerprints.ALIGNED:
                verdicts["gap"] += 1
            else:
                verdicts["clean"] += 1

    taken = _utc(snap["taken_at"])
    iso = lambda t: t.isoformat().replace("+00:00", "Z") if t else None
    return {
        "date": date.today().isoformat(),
        "done_today": done_today,
        "target": pia.DAILY_TARGET,
        "remaining_today": max(0, pia.DAILY_TARGET - done_today),
        "queue": queue,
        "verdicts": verdicts,
        "per_day": [{"day": d, "leads": n} for d, n in sorted(per_day.items(), reverse=True)],
        "today_first": iso(min(touched_today)) if touched_today else None,
        "today_last": iso(max(touched_today)) if touched_today else None,
        "in_flight": in_flight,
        "paused": pia.PAUSE_FILE.exists(),
        "run_at": os.environ.get("PARTNER_RUN_AT") or None,
        "snapshot_at": iso(taken),
        "snapshot_stale": (now - taken).total_seconds() > STALE_AFTER_S,
    }


@app.get("/api/partner/rows", tags=["partner"])
def partner_rows(status: Optional[str] = None, q: str = "",
                 limit: int = Query(100, le=500)):
    """Most recently touched partner rows, newest first."""
    rows = _snapshot()["rows"]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r["company"] or "").lower()
                or needle in (r["website"] or "").lower()]
    # Newest first, untouched rows last, ties by Id. reverse=True keeps the
    # sort stable, so the Id order from the first pass survives among ties.
    rows = sorted(rows, key=lambda r: r["id"])
    rows.sort(key=lambda r: r["scraped_at"] or "", reverse=True)
    return rows[:limit]


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

    @app.get("/ops", include_in_schema=False)
    def ops():
        """Daily activity: throughput, queue depth and the stage run log."""
        return FileResponse(UI_DIR / "ops.html")

    @app.get("/partner", include_in_schema=False)
    def partner():
        """Partner-import activity: queue, today's quota, verdicts, recent rows."""
        return FileResponse(UI_DIR / "partner.html")
