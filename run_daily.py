"""
Daily pipeline runner. Chains every stage until the day's lead target is met.

    python run_daily.py                    # run toward the default target
    python run_daily.py --target 500
    python run_daily.py --dry-run          # show the plan, run nothing
    python run_daily.py --status           # where today stands, then exit

Built to survive the machine losing power mid-run:

* Progress is DERIVED, never stored. "How many leads today" is counted from
  delivered_at in the database, so a restart continues toward the target
  instead of starting over or double-counting. There is no counter to drift.
* Every stage writes per company, so a crash costs only the few in flight.
  Postgres runs fsync=on, so anything committed is on disk.
* Stages are re-entrant: each reads a status and writes the next, so simply
  running the script again resumes exactly where it stopped.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import config
import db

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / "venv" / "bin" / "python")

DEFAULT_TARGET = 500
# Screening batch per cycle. Sized so a cycle is ~20 minutes: long enough to
# amortise browser startup, short enough that a crash loses little.
SCREEN_BATCH = 400
STATE_ORDER = ("maharashtra", "delhi", "chandigarh", "karnataka",
               "gujarat", "uttar pradesh")   # best resolution rate first


def leads_today():
    """Derived from the data, never a stored counter."""
    with db.get_cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS n FROM companies
                       WHERE delivered_at::date = CURRENT_DATE""")
        return cur.fetchone()["n"]


def pending(status, where=None):
    with db.get_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM companies WHERE status=%s"
                    + (f" AND {where}" if where else ""), (status,))
        return cur.fetchone()["n"]


def log_run(stage, started, code, records_in, note=""):
    with db.get_cursor() as cur:
        cur.execute("""INSERT INTO pipeline_runs
                       (stage, started_at, finished_at, exit_code, records_in,
                        leads_after, note)
                       VALUES (%s, %s, NOW(), %s, %s, %s, %s)""",
                    (stage, started, code, records_in, leads_today(), note[:500]))


def run(stage, args, dry_run=False):
    """Runs one stage as a subprocess so a hard crash cannot take the loop down."""
    cmd = [PY, "-W", "ignore", "-u", str(ROOT / "stages" / stage)] if stage.endswith(".py") \
          and (ROOT / "stages" / stage).exists() else [PY, "-W", "ignore", "-u", str(ROOT / stage)]
    cmd += args
    if dry_run:
        print("   would run:", " ".join(cmd[3:]))
        return 0
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"   -> {Path(cmd[4]).name} {' '.join(args)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    tail = (proc.stdout or "").strip().splitlines()
    for line in tail[-3:]:
        print(f"      {line}", flush=True)
    if proc.returncode != 0:
        print(f"      FAILED rc={proc.returncode}: {(proc.stderr or '')[-300:]}", flush=True)
    log_run(Path(cmd[4]).stem, started, proc.returncode, None,
            tail[-1] if tail else "")
    return proc.returncode


def cycle(dry_run):
    """One pass through the pipeline. Each stage feeds the next."""
    # Screen new companies, best-yielding states first.
    if pending("discovered") > 0:
        for state in STATE_ORDER:
            if pending("discovered", f"state_code='{state}'") == 0:
                continue
            run("02_resolve_domain.py",
                ["--limit", str(SCREEN_BATCH // len(STATE_ORDER)),
                 "--no-escalate", "--where", f"state_code='{state}'"], dry_run)

    if pending("resolved") or dry_run:
        run("03_scrape_playwright.py", ["--limit", "500", "--concurrency", "6"], dry_run)
    if pending("scraped", "has_cmp = false AND consent_gap_score > 0") or dry_run:
        run("05_classify_scale.py", ["--limit", "500"], dry_run)
    if pending("classified") or dry_run:
        run("06_enrich_contact_apollo.py", ["--limit", "500"], dry_run)
    if pending("contact_done") or dry_run:
        run("07_export_crm.py", [], dry_run)
    if pending("exported", "delivered_at IS NULL") or dry_run:
        run("crm_push.py", ["--limit", "500"], dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--max-cycles", type=int, default=40)
    args = ap.parse_args()

    done = leads_today()
    if args.status:
        print(f"leads delivered today : {done}/{args.target}")
        print(f"queue: discovered {pending('discovered'):,} | resolved {pending('resolved')} "
              f"| scraped {pending('scraped')} | classified {pending('classified')} "
              f"| exported-undelivered {pending('exported', 'delivered_at IS NULL')}")
        return

    print(f"target {args.target}/day — {done} already delivered today "
          f"({'resuming' if done else 'starting fresh'})\n")

    for i in range(1, args.max_cycles + 1):
        done = leads_today()
        if done >= args.target:
            print(f"\nTARGET MET: {done}/{args.target} leads delivered today")
            return
        if pending("discovered") == 0 and pending("resolved") == 0 \
                and pending("scraped") == 0 and pending("classified") == 0:
            print("\nno work left in the queue")
            return
        print(f"cycle {i}  ({done}/{args.target} delivered)", flush=True)
        cycle(args.dry_run)
        if args.dry_run:
            print("\ndry run — nothing executed")
            return

    print(f"\nstopped after {args.max_cycles} cycles at {leads_today()}/{args.target}")


if __name__ == "__main__":
    main()
