"""
The scheduled jobs.

    python -m campaign.jobs prepare      [--dry-run] [--limit N] [--sample N] [--seed S] [--production]
    python -m campaign.jobs housekeeping [--dry-run]

Every job goes through run_job(): kill switch first, then a Postgres advisory lock
named after the job (a second run exits at once), then one bounded pass that keeps no
state in memory. Exit status is 0 for ran / killed / locked: a skipped run is normal.

prepare = intake, then stages 2-9 (free checks, domain, scan, score gate, report, contact ranking,
NeverBounce, drafting). The test cohort (--sample) runs every stage but never
writes to the CRM; production is only processed when enabled. housekeeping runs the nightly
reconciliation and the mirror audit.
"""
import argparse
import json
import sys
import time
from datetime import datetime

from . import flags, locks, settings, stages, status


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def funnel(only_test):
    """{status: companies} for the test cohort (True), production (False) or everything (None)."""
    import db
    where, params = "TRUE", []
    if only_test is not None:
        where, params = "is_test = %s", [only_test]
    scope, scope_params = status.scope_sql()          # a scoped run reports only the companies it was given
    with db.get_cursor() as cur:
        cur.execute(f"SELECT status, COUNT(*) n FROM ae_companies WHERE {where}{scope} AND status <> 'legacy' "
                    f"GROUP BY status ORDER BY n DESC", [*params, *scope_params])
        return {r["status"]: r["n"] for r in cur.fetchall()}


def run_stages(only_test, dry_run=False, limit=None, first=2, last=9, write_ae=False):
    """
    Stages `first`..`last` in order for one cohort. A stage that is not built yet is skipped. Returns [{stage, name, seconds, counts, funnel}], the funnel being the company
    count per status after the stage. With dry_run every stage only reports what it would do.
    """
    out = []
    for stage in stages.STAGES:
        if not (first <= stage.number <= last) and not (write_ae and stage.number == 10 and not only_test):
            continue
        if not stage.implemented:
            continue
        t0 = time.monotonic()
        counts = stage.run(limit=limit, dry_run=dry_run, only_test=only_test)
        fun = funnel(only_test)
        out.append({"stage": stage.number, "name": stage.name, "seconds": round(time.monotonic() - t0, 2),
                    "counts": counts, "funnel": fun})
        log(f"stage {stage.number} {stage.name} ({'test' if only_test else 'production'}"
            f"{', dry run' if dry_run else ''}): {json.dumps(counts, sort_keys=True, default=str)} "
            f"-> funnel {json.dumps(fun, sort_keys=True)}")
    return out


def prepare(dry_run=False, limit=None, sample=None, seed=None, production=False, ids=None, adopt_ids=None,
            write_ae=False, reader=None):
    """
    Intake, then stages 2-9. The test cohort (--sample) is taken in and run through every stage
    (scans, report PDFs, R2 uploads, the model server, NeverBounce per its mode) but never writes to
    the CRM. Production companies are taken in, and only processed by stages 2-8 when
    settings.PRODUCTION_ENABLED or --production says so.
    """
    from . import crm_read, intake
    own = reader is None
    reader = reader or crm_read.CrmReader()
    result = {"intake": [], "stages": {}}
    try:
        result["intake"].append(intake.run_intake(reader, dry_run=dry_run, limit=limit,
                                                    only_ids=set(ids) if ids else None))
        if sample:
            result["intake"].append(intake.run_intake(reader, sample=sample, seed=seed, dry_run=dry_run))
        for r in result["intake"]:
            log(f"intake {r['cohort']}{' (dry run)' if dry_run else ''}: {json.dumps(r, sort_keys=True)}")
        if not dry_run:
            filled = intake.backfill_details(reader)
            if any(filled.values()):
                log(f"backfilled details from the CRM: {json.dumps(filled)}")
    finally:
        if own:
            reader.close()
    if adopt_ids:
        from . import adopt, crm_read as _cr
        r2 = reader or _cr.CrmReader()
        try:
            result["adopted"] = adopt.adopt(r2, adopt_ids)
        finally:
            if reader is None:
                r2.close()
        log(f"adopted: {json.dumps(result['adopted'], default=str)}")
        production = True
    named = list(ids or []) + list(adopt_ids or [])
    if dry_run and named:
        # What a real run would do, per stage, limited to the named companies. Nothing is in Postgres yet, so the
        # per-stage dry runs below see no rows; this plan says which companies each stage would be handed.
        take = sorted(result["intake"][0].get("would_take_ids", [])) if result["intake"] else []
        present = sorted(named) if adopt_ids else take
        result["dry_run_plan"] = {
            "scope": sorted(named), "intake_would_take": take,
            "stages": [{"stage": st.number, "name": st.name, "would_touch": present,
                        "only_if": "they are still in the pipeline after the stage before" if st.number > 2 else "taken in"}
                       for st in stages.STAGES
                       if 2 <= st.number <= 9 or (st.number == 10 and write_ae)]}
        log(f"dry run plan (scope {sorted(named)}; no other company can appear): "
            + json.dumps(result["dry_run_plan"]["stages"]))
    if sample or production or settings.PRODUCTION_ENABLED:
        if sample:
            result["stages"]["test"] = run_stages(True, dry_run, limit)
        if production or settings.PRODUCTION_ENABLED:
            named = list(ids or []) + list(adopt_ids or [])
            with status.only_companies(named):       # --ids / --adopt-ids mean "only these", in every stage
                result["stages"]["production"] = run_stages(False, dry_run, limit, write_ae=write_ae)
    else:
        log("stages 2-8 skipped for production: not enabled (AE_PRODUCTION_ENABLED or --production)")
    return result


def housekeeping(dry_run=False, reader=None, **_):
    """
    02:00 IST. Reconciliation is real (it is intake) and so is the mirror audit (read-only: it
    reports where the CRM's ScrapeStatus / ReportStatus disagree with ae_companies and fixes
    nothing). Draft expiry and cache pruning are stubs.
    """
    from . import crm_read, intake, mirror
    own = reader is None
    reader = reader or crm_read.CrmReader()
    try:
        r = intake.reconcile(reader, dry_run=dry_run)
        log(f"reconcile{' (dry run)' if dry_run else ''}: {json.dumps(r, sort_keys=True)}")
        bad = mirror.audit(reader)
        log(f"mirror audit: {len(bad)} disagreement(s)" + (f": {json.dumps(bad[:20], default=str)}" if bad else ""))
        return r
    finally:
        if own:
            reader.close()


JOBS = {"prepare": prepare, "housekeeping": housekeeping}


def run_job(name, fn=None, **options):
    """
    Returns "killed", "locked" or "ok". `fn` defaults to the registered job and is
    called with the options as keyword arguments.
    """
    if name not in JOBS:
        raise ValueError(f"unknown job {name!r}")
    if flags.kill_switch_on():
        log(f"{name}: kill switch is on, not running")
        return "killed"
    with locks.job_lock(name) as got:
        if not got:
            log(f"{name}: another run holds the lock, exiting")
            return "locked"
        (fn or JOBS[name])(**options)
        return "ok"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", choices=sorted(JOBS))
    ap.add_argument("--dry-run", action="store_true", help="read the CRM, count, write nothing")
    ap.add_argument("--limit", type=int, help="most companies to take in this run")
    ap.add_argument("--sample", type=int, help="also take N already-processed companies as a test cohort")
    ap.add_argument("--seed", type=int, help="make --sample reproducible")
    ap.add_argument("--ids", type=lambda v: [int(x) for x in v.split(",") if x.strip()],
                    help="take in only these CRM Ids (comma separated). Stages 2-9 process every production company "
                         "in the database, so use this with a database that holds only the demo leads")
    ap.add_argument("--adopt-ids", type=lambda v: [int(x) for x in v.split(",") if x.strip()],
                    help="adopt these CRM Ids that already have a generated report: their report columns stay as they are; "
                         "contact ranking, NeverBounce and drafting run on them")
    ap.add_argument("--write-ae", action="store_true",
                    help="after drafting, write the draft into the CRM AE_ columns (production companies only)")
    ap.add_argument("--production", action="store_true",
                    help="also run stages 2-8 for production companies (otherwise only when AE_PRODUCTION_ENABLED=1)")
    args = ap.parse_args(argv)
    options = {"dry_run": args.dry_run}
    if args.job == "prepare":
        options.update(limit=args.limit, sample=args.sample, seed=args.seed, production=args.production, ids=args.ids,
                       adopt_ids=args.adopt_ids, write_ae=args.write_ae)
    outcome = run_job(args.job, **options)
    log(f"{args.job}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
