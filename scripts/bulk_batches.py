#!/usr/bin/env python3
"""
Runs the held-out express batches (GoodFirms, then Clutch) to completion, hands-off.

    nohup python3 scripts/bulk_batches.py >> logs/bulk_batches.log 2>&1 &     # start (or run it inside tmux)
    nohup python3 scripts/bulk_batches.py --batches clutch >> logs/bulk_batches.log 2>&1 &    # only Clutch
    python3 scripts/bulk_batches.py --status                                    # counts per batch and status
    touch logs/bulk_batches.stop                                                # stop after the run in progress

Two files in logs/ (git-ignored): bulk_batches.log (what the loop did, one line per stage and per pass) and
bulk_batches_records.csv (one row per company each time its state changed: id, name, domain, status and why, band, score,
report link, the contact the draft is addressed to, draft status and template version; a company can appear several times
as it moves on, and its last row is where it ended up).

Each pass is `prepare --batch NAME --production --write-ae --limit 100` with role addresses allowed: it takes in up to
100 new companies of the batch and moves everything in the batch one stage on (free checks, domain, scan, gate, report,
primary contact, draft, CRM write), every company committed as it finishes, so a stop or a crash costs nothing. A batch is
done when nothing is left in a working status and nothing is left to take in; Clutch starts only after GoodFirms is.

It does not run while the scheduled `daily` job does: from 06:05 to 06:40 IST it waits, and after that for as long as the
`daily` job holds its Postgres lock (checked read-only, so it can never make the scheduler think another run is active).
A company whose scan timed out is retried after the pipeline's own wait (15, then 60 minutes); when only those are left
the loop sleeps until they are due. Three passes in a row with no change and nothing waiting = stuck: it says so and
moves on.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = str(ROOT / "scripts" / "run.sh")
STOP = ROOT / "logs" / "bulk_batches.stop"
RECORDS = ROOT / "logs" / "bulk_batches_records.csv"
RECORD_COLUMNS = ["recorded_at", "batch", "company_id", "company_name", "domain", "status", "status_reason", "band", "score",
                  "gap_count", "report_url", "scan_attempts", "failure_category", "contact_email", "draft_status",
                  "template_version", "written_to_crm_at", "updated_at"]

BATCHES = ["goodfirms", "clutch"]
LIMIT = 100                      # companies taken in, and per-stage work, per pass
ENV = ["-e", "AE_ROLE_LOCAL_PARTS=__none__"]      # role addresses (info@, sales@ ...) are the published contacts here
IST = timezone(timedelta(hours=5, minutes=30))
PAUSE_FROM, PAUSE_UNTIL = (6, 5), (6, 40)        # IST: around the 06:30 daily job
WORKING = ("pending", "checked", "scanned", "gate_passed", "reported", "contact_ranked", "verified", "drafted")
MAX_FAILED_PASSES = 5
MAX_RETRY_SLEEP = 25 * 60

PROBE = r'''
import json, sys
import db
from campaign import crm_read, locks, settings
want = sys.argv[1:]
out = {"daily_running": locks.job_running("daily"), "batches": {}}
conn = crm_read.connect() if want else None
cur = conn.cursor() if want else None
for name in want:
    marks = ", ".join(["%s"] * len(settings.BATCHES[name]))
    sql = f"SELECT COUNT(*) FROM dbo.CRM_Scrapper WHERE LOWER(Source) IN ({marks})"
    crm_read.assert_read_only(sql)
    cur.execute(sql, tuple(settings.BATCHES[name]))
    total = cur.fetchone()[0]
    with db.get_cursor() as k:
        k.execute("SELECT status, COUNT(*) n FROM ae_companies WHERE batch = %s GROUP BY status", (name,))
        counts = {r["status"]: r["n"] for r in k.fetchall()}
        k.execute("""SELECT MIN(next_retry_at) m FROM ae_companies
                     WHERE batch = %s AND status = 'checked' AND next_retry_at IS NOT NULL""", (name,))
        retry = k.fetchone()["m"]
        k.execute("SELECT EXTRACT(EPOCH FROM (%s - NOW())) s", (retry,)) if retry else None
        wait = float(k.fetchone()["s"]) if retry else None
    out["batches"][name] = {"in_crm": total, "counts": counts, "retry_in_s": wait}
if conn:
    conn.close()
print("PROBE " + json.dumps(out))
'''


FETCH = r'''
import json, sys
import db
batch, since = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
with db.get_cursor() as k:
    k.execute("SELECT NOW() AS now")
    now = k.fetchone()["now"]
    rows = []
    if since:
        k.execute("""SELECT a.company_id, a.company_name, a.domain, a.status, a.status_reason, a.band, a.score, a.gap_count,
                            a.report_url, a.scan_attempts, a.failure_category, a.updated_at,
                            c.email AS contact_email, d.draft_status, d.template_version, d.written_to_crm_at
                     FROM ae_companies a
                     LEFT JOIN ae_contacts c ON c.contact_id = a.chosen_contact_id
                     LEFT JOIN ae_drafts d ON d.company_id = a.company_id
                     WHERE a.batch = %s AND a.updated_at > %s::timestamptz ORDER BY a.updated_at, a.company_id""", (batch, since))
        rows = [{k_: (v.isoformat() if hasattr(v, "isoformat") else v) for k_, v in r.items()} for r in k.fetchall()]
print("RECORDS " + json.dumps({"now": now.isoformat(), "rows": rows}))
'''


def log(msg):
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


def probe(names=None):
    """{"daily_running": bool, "batches": {name: {in_crm, counts, retry_in_s}}}, read-only."""
    names = BATCHES if names is None else names
    p = subprocess.run([RUN, "campaign", "python", "-", *names], input=PROBE, text=True,
                       capture_output=True, timeout=300)
    for line in p.stdout.splitlines():
        if line.startswith("PROBE "):
            return json.loads(line[6:])
    raise RuntimeError(f"probe failed (exit {p.returncode}): {(p.stdout + p.stderr)[-400:]}")


def fetch_records(batch, since):
    """{"now": ts, "rows": [...]} : the batch's companies updated after `since` (an ISO timestamp); "" = just the clock."""
    p = subprocess.run([RUN, "campaign", "python", "-", batch, since], input=FETCH, text=True, capture_output=True,
                       timeout=300)
    for line in p.stdout.splitlines():
        if line.startswith("RECORDS "):
            return json.loads(line[8:])
    raise RuntimeError(f"records fetch failed (exit {p.returncode}): {(p.stdout + p.stderr)[-400:]}")


def write_records(batch, rows, path=None):
    """Appends one CSV row per changed company (header on first write). Returns how many were written."""
    import csv
    path = Path(path or RECORDS)
    path.parent.mkdir(exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=RECORD_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({**r, "recorded_at": stamp, "batch": batch})
    return len(rows)


def in_pause_window(now):
    t = (now.hour, now.minute)
    return PAUSE_FROM <= t < PAUSE_UNTIL


def wait_for_scheduler():
    announced = False
    while True:
        if STOP.exists():
            return
        now = datetime.now(IST)
        busy = in_pause_window(now)
        if not busy:
            try:
                busy = probe([])["daily_running"]
            except Exception as exc:                       # cannot tell: assume it may be running, ask again shortly
                log(f"could not check for the daily job ({exc}); waiting a minute")
                busy = True
        if not busy:
            if announced:
                log("the scheduled job is not running; resuming")
            return
        if not announced:
            log("pausing: the 06:30 scheduled job window / the daily job is running")
            announced = True
        time.sleep(60)


def run_pass(batch):
    """One prepare pass. Returns {"outcome": ok|locked|killed|failed, "intake_new": n|None}."""
    cmd = [RUN, *ENV, "campaign", "python", "-m", "campaign.jobs", "prepare", "--batch", batch, "--production",
           "--write-ae", "--limit", str(LIMIT)]
    res = {"outcome": "failed", "intake_new": None}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        line = line.rstrip()
        m = re.search(r"intake prod: (\{.*\})", line)
        if m:
            try:
                res["intake_new"] = json.loads(m.group(1)).get("companies_new")
            except ValueError:
                pass
        m = re.search(r"prepare: (\w+)\s*$", line)
        if m:
            res["outcome"] = {"ok": "ok", "locked": "locked", "killed": "killed"}.get(m.group(1), "failed")
        if re.search(r"stage \d+ \w+ \(production\)", line):
            log(re.sub(r"^\[[^\]]*\] ", "", line)[:230])          # one trimmed line per stage
        elif "Traceback" in line or "Error" in line[:80]:
            log(line[:300])
    proc.wait()
    if proc.returncode not in (0, None) and res["outcome"] == "ok":
        res["outcome"] = "failed"
    return res


def work_left(counts):
    return sum(counts.get(s, 0) for s in WORKING)


def run_batch(batch):
    log(f"=== batch {batch} ===")
    prev, stalls, failures = None, 0, 0
    since = fetch_records(batch, "")["now"]        # records start from now: what this loop does, not what came before
    while True:
        if STOP.exists():
            log("stop file found: stopping before the next pass")
            return "stopped"
        wait_for_scheduler()
        if STOP.exists():
            continue
        res = run_pass(batch)
        if res["outcome"] == "killed":
            log("the kill switch is on: stopping")
            return "stopped"
        if res["outcome"] == "locked":
            log("another prepare run holds the lock; waiting 2 minutes")
            time.sleep(120)
            continue
        if res["outcome"] == "failed":
            failures += 1
            log(f"pass failed ({failures}/{MAX_FAILED_PASSES}); waiting a minute")
            if failures >= MAX_FAILED_PASSES:
                log("too many failed passes in a row: stopping")
                return "failed"
            time.sleep(60)
            continue
        failures = 0
        try:
            got = fetch_records(batch, since)
            n = write_records(batch, got["rows"])
            if got["rows"]:
                since = max(r["updated_at"] for r in got["rows"])
            kinds = {}
            for r in got["rows"]:
                kinds[r["status"]] = kinds.get(r["status"], 0) + 1
            log(f"recorded {n} change(s) in {RECORDS.name}: {json.dumps(kinds, sort_keys=True)}")
        except Exception as exc:                    # the records are a log: never let them stop the run
            log(f"could not write the records this pass ({exc}); the next pass will include them")
        info = probe([batch])["batches"][batch]
        counts, left = info["counts"], work_left(info["counts"])
        log(f"{batch}: in CRM {info['in_crm']}, taken in {sum(counts.values())}, working {left}, "
            f"counts {json.dumps(counts, sort_keys=True)}")
        if (res["intake_new"] or 0) == 0 and left == 0:
            log(f"=== batch {batch} cleared ===")
            return "cleared"
        sig = (json.dumps(counts, sort_keys=True), res["intake_new"])
        if sig == prev:
            if info["retry_in_s"] is not None and info["retry_in_s"] > 0:
                nap = min(info["retry_in_s"] + 10, MAX_RETRY_SLEEP)
                log(f"only scan retries are left; sleeping {int(nap)} s until the first is due")
                time.sleep(nap)
                continue
            stalls += 1
            log(f"nothing moved ({stalls}/3)")
            if stalls >= 3:
                log(f"=== batch {batch} is stuck: {left} companies are in a working status and none can move ===")
                return "stuck"
        else:
            stalls = 0
        prev = sig


def pick_batches(arg):
    """The batches to run, in the order given (default: all, GoodFirms first). Unknown names are an error."""
    if not arg:
        return list(BATCHES)
    names = [n.strip() for n in arg.split(",") if n.strip()]
    unknown = [n for n in names if n not in BATCHES]
    if not names or unknown:
        raise SystemExit(f"--batches: unknown {unknown or arg!r}; choose from {BATCHES}")
    return names


def status():
    info = probe()["batches"]
    for name in BATCHES:
        b = info[name]
        taken = sum(b["counts"].values())
        print(f"{name}: in CRM {b['in_crm']}, taken in {taken}, still to take in {b['in_crm'] - taken}, "
              f"working {work_left(b['counts'])}")
        print("   " + ", ".join(f"{k} {v}" for k, v in sorted(b["counts"].items(), key=lambda kv: -kv[1])))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="print counts per batch and status, then exit")
    ap.add_argument("--batches", help="comma-separated batches to run, in order (default: goodfirms,clutch)")
    args = ap.parse_args()
    chosen = pick_batches(args.batches)
    if args.status:
        status()
        return 0
    STOP.parent.mkdir(exist_ok=True)
    if STOP.exists():
        STOP.unlink()
    log(f"bulk run starting: {' then '.join(chosen)}, {LIMIT} companies per pass")
    results = {}
    for batch in chosen:
        results[batch] = run_batch(batch)
        if results[batch] == "stopped" or results[batch] == "failed":
            break
    log(f"bulk run finished: {json.dumps(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
