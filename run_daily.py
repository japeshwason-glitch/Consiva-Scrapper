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
# The interpreter running this file, so the stages inherit the same
# environment. Locally that is venv/bin/python; in the container it is the
# image's python and no venv exists.
PY = sys.executable

DEFAULT_TARGET = 500          # leads delivered to the CRM: a company with a
                              # consent gap, contact name or not

# Six workers, measured rather than guessed. The model's throughput curve:
#
#   workers   per-call   throughput   vs sequential
#         1      2.20s      0.46/s          1.0x
#         2      2.69s      0.74/s          1.6x
#         4      4.31s      0.93/s          2.0x
#         6      4.30s      1.29/s          2.8x   <- knee
#         8      5.71s      1.29/s          2.8x
#        12      8.66s      1.23/s          2.7x
#
# Past six, per-call latency keeps climbing while total throughput flattens and
# then falls -- the extra parallelism is fully cancelled by each request getting
# slower. Six is where the trade stops paying.
#
# Sequential costs 2.8x: 500 leads would need ~20.6h of model time
# against ~7.3h at six, leaving no margin for a slow batch.
WORKERS = 6
SCRAPE_CONCURRENCY = 6
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


def scraped_today():
    """Websites actually loaded in a browser today -- the expensive operation."""
    with db.get_cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS n FROM companies
                       WHERE consent_gap_score IS NOT NULL
                         AND updated_at::date = CURRENT_DATE""")
        return cur.fetchone()["n"]


def pending(status, where=None):
    with db.get_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM companies WHERE status=%s"
                    + (f" AND {where}" if where else ""), (status,))
        return cur.fetchone()["n"]


def queue_signature():
    """
    Counts at every status that work moves through. Two identical signatures
    across a cycle mean nothing moved anywhere, which is the only reliable
    signal that another cycle is pointless.
    """
    # One GROUP BY, not seven counts. Each count is a scan of 1.4M rows
    # (45ms), while the grouped form answers for every status at once (37ms) --
    # and this runs twice per cycle, so the naive version cost ~0.6s a cycle
    # to learn nothing extra.
    with db.get_cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM companies GROUP BY status")
        counts = {r["status"]: r["n"] for r in cur.fetchall()}
    return tuple(counts.get(st, 0) for st in
                 ("discovered", "resolved", "scraped", "classified",
                  "contact_done", "contact_failed", "exported"))


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


def cycle(dry_run, workers=WORKERS, skip_contacts=False):
    """
    One pass through the pipeline. Each stage feeds the next.

    Returns True if contact enrichment reported its daily allowance spent, so
    the caller can skip it for the rest of the day instead of paying its
    rate-limit waits on every cycle for nothing.
    """
    budget_spent = False
    # Screen new companies, best-yielding states first.
    if pending("discovered") > 0:
        for state in STATE_ORDER:
            if pending("discovered", f"state_code='{state}'") == 0:
                continue
            run("02_resolve_domain.py",
                ["--limit", str(SCREEN_BATCH // len(STATE_ORDER)),
                 "--no-escalate", "--workers", str(workers),
                 "--where", f"state_code='{state}'"], dry_run)

    if pending("resolved") or dry_run:
        run("03_scrape_playwright.py",
            ["--limit", "500", "--concurrency", str(SCRAPE_CONCURRENCY)], dry_run)
    if pending("scraped", "has_cmp = false AND consent_gap_score > 0") or dry_run:
        run("05_classify_scale.py", ["--limit", "500"], dry_run)
    # Contact enrichment is additive -- stage 07 exports a lead whether or not
    # this ran -- so a spent allowance is a reason to skip it, never to stall.
    if not skip_contacts and (pending("classified") or dry_run):
        rc = run("06_enrich_contact_apollo.py", ["--limit", "500"], dry_run)
        if rc == 75:
            budget_spent = True
            print("   contact allowance spent for the day; "
                  "leads still export without a name", flush=True)
    # Stage 07 exports contact_done AND contact_failed -- a company with a
    # consent gap is a lead whether or not Apollo had a person for it, and
    # Apollo returns nothing for most small Indian firms. Gating on
    # contact_done alone means a cycle where every lookup misses exports
    # nothing at all, which is the same bug stage 07 itself was fixed for.
    gap = "has_cmp = false AND consent_gap_score > 0"
    if pending("contact_done", gap) or pending("contact_failed", gap) or dry_run:
        run("07_export_crm.py", [], dry_run)
    if pending("exported", "delivered_at IS NULL") or dry_run:
        run("crm_push.py", ["--limit", "500"], dry_run)
    return budget_spent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET,
                    help="stop once this many leads have reached the CRM today")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help="parallel workers per stage; 1 keeps the local model "
                         "sequential, which is what it serves best")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--max-cycles", type=int, default=40)
    args = ap.parse_args()

    done = leads_today()
    if args.status:
        print(f"leads delivered today : {done}/{args.target}")
        print(f"websites scraped today: {scraped_today()} (no cap)")
        print(f"queue: discovered {pending('discovered'):,} | resolved {pending('resolved')} "
              f"| scraped {pending('scraped')} | classified {pending('classified')} "
              f"| exported-undelivered {pending('exported', 'delivered_at IS NULL')}")
        return

    print(f"target {args.target} leads/day, no scrape cap, "
          f"{args.workers} worker(s)")
    print(f"{done} leads and {scraped_today()} scrapes already done today "
          f"({'resuming' if done or scraped_today() else 'starting fresh'})\n")

    skip_contacts = False
    stalled = 0
    for i in range(1, args.max_cycles + 1):
        done = leads_today()
        if done >= args.target:
            print(f"\nTARGET MET: {done}/{args.target} leads delivered today")
            return
        scraped = scraped_today()
        if pending("discovered") == 0 and pending("resolved") == 0 \
                and pending("scraped") == 0 and pending("classified") == 0:
            print("\nno work left in the queue")
            return
        print(f"cycle {i}  ({done}/{args.target} leads, "
              f"{scraped} scraped today)", flush=True)

        before = (done, scraped, queue_signature())
        budget_spent = cycle(args.dry_run, args.workers, skip_contacts)
        skip_contacts = skip_contacts or budget_spent
        if args.dry_run:
            print("\ndry run — nothing executed")
            return

        # A cycle that delivers nothing AND leaves every queue unchanged has
        # nothing left to try: every stage is either empty or failing the same
        # way. Spinning through the remaining cycles would just repeat it, so
        # stop and say so rather than burn the night looking busy.
        after = (leads_today(), scraped_today(), queue_signature())
        if after == before:
            stalled += 1
            if stalled >= 2:
                print(f"\nSTALLED at {after[0]}/{args.target} leads -- two "
                      f"cycles changed nothing. Queue: {queue_signature()}")
                print("Nothing is retryable right now; check the stage output "
                      "above. The next scheduled run will pick up where this "
                      "stopped -- no state is lost.")
                return
        else:
            stalled = 0

    print(f"\nstopped after {args.max_cycles} cycles at {leads_today()}/{args.target}")


if __name__ == "__main__":
    main()
