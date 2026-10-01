"""
One-off: rescan every finished PartnerImport row in dbo.CRM_Scrapper with the
fixed cookie classifier (fingerprints 2026-10-cookie-fix) and overwrite its
consent findings. partner_import_analyse.py is not used or changed.

    python scripts/rescan_partner_consent.py --dry-run --limit 6   # scan, write nothing
    python scripts/rescan_partner_consent.py                       # the run (resumable)
    python scripts/rescan_partner_consent.py --report              # summary from progress

Partner rows keep no cookie records, so unlike the MCA rows they cannot be
re-classified offline -- each site is loaded again with stage 03's
scrape_one() (no LLM), 6 at a time, 120 s per attempt, 2 attempts.

Each scan attempt and each CRM write runs in its own child process, killed
when its time is up. A CRM write on a connection the server had dropped blocked
inside FreeTDS for good -- pymssql's own timeout never fired -- and since it ran
on the event loop it froze the whole run (py-spy: MainThread in write_row).

Overwrites ONLY GapScore, TrackersPreConsent, ThirdPartiesCount, CookieBanner,
CollectsViaForm, ConsentFinding and UpdatedAt, with values built by
partner_import_analyse.values_for() exactly as the partner job builds them.
ScrapeStatus / ScrapedAt are untouched, so the daily partner quota is not
affected. A site that fails both attempts keeps its old values.

Resumable: the first start backs up the target rows and records them in
data/partner_rescan_state.json; every finished site is appended to
data/partner_rescan_progress.jsonl. A re-run skips rows already rescanned and
retries rows that failed. Delete both files to start a new run.

The progress file also keeps each site's classified cookie records (never
values) and scan inputs, so a later classification change is applied offline:
on every start, rows rescanned under an older FINGERPRINTS_VERSION are
re-classified from their saved records and rewritten, with no page load.

Stops cleanly when data/partner.pause exists: in-flight sites finish, nothing
new starts.
"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import config
import crm_push
import fingerprints as fp
import partner_import_analyse as pia
from reclassify_cookies import form_from_row, legacy_analysis, legacy_consent_score

s3 = pia._stage03

CONCURRENCY = 6
SITE_TIMEOUT_S = int(os.environ.get("RESCAN_SITE_TIMEOUT_S", "120"))
ATTEMPTS = 2
WRITE_TIMEOUT_S = 45
WRITE_ATTEMPTS = 3
TABLE = config.CRM_DB_TABLE
DATA = ROOT / "data"
STATE = DATA / "partner_rescan_state.json"
PROGRESS = DATA / "partner_rescan_progress.jsonl"

OVERWRITE = ("GapScore", "TrackersPreConsent", "ThirdPartiesCount",
             "CookieBanner", "CollectsViaForm", "ConsentFinding")
# What the progress file keeps of each scan: the score inputs and the cookie
# records. footer_phone / footer_address are left out deliberately.
KEEP = ("final_url", "final_domain", "page_title", "has_cmp", "cmp_name",
        "has_cookie_banner", "has_privacy_policy", "pre_consent_cookies",
        "cookies_total", "tracking_cookie_count", "third_party_count",
        "third_party_domains", "max_cookie_lifetime_days", "consent_gap_score",
        "consent_gap_summary", "has_data_form", "form_field_types",
        "form_consent_checkbox", "form_third_party_hosts", "form_embed_provider",
        "tech_analytics_tools", "tech_marketing_tools")


# --- the classifier as it was, to separate the fix from site drift -----------
_OLD_ADVERTISING = ("_fbp", "_fbc", "fr", "IDE", "test_cookie", "NID", "DSID", "_gcl",
                    "li_sugr", "bcookie", "lidc", "UserMatchHistory", "_uetsid",
                    "_uetvid", "YSC", "VISITOR_INFO1_LIVE")
_OLD_REPLAY = ("_hj", "_hjs", "_clck", "_clsk", "MUID")
_OLD_TRACKING = ("_ga", "_gid", "_gat", "__utm", "_gcl", "_dc_gtm", "_fbp", "_fbc", "fr",
                 "_hjs", "_hj", "_clck", "_clsk", "MUID", "__hstc", "hubspotutk",
                 "__hssrc", "__hssc", "_lfa", "li_sugr", "bcookie", "lidc",
                 "UserMatchHistory", "IDE", "test_cookie", "NID", "DSID", "mp_",
                 "ajs_", "amplitude_", "_pk_", "_omappvp", "_uetsid", "_uetvid",
                 "YSC", "VISITOR_INFO1_LIVE")


def legacy_category(name, third_party):
    if any(name.startswith(p) for p in _OLD_ADVERTISING):
        return "advertising"
    if any(name.startswith(p) for p in _OLD_REPLAY):
        return "session_replay"
    if any(name.startswith(p) for p in _OLD_TRACKING):
        return "analytics"
    return "third_party" if third_party else "functional"


def legacy_score(data):
    """The GapScore the pre-fix code would have produced from this same scan."""
    records = [{**r, "category": legacy_category(r["name"], r["party"] == "third")}
               for r in data["pre_consent_cookies"]]
    return legacy_consent_score(data["has_cmp"], legacy_analysis(records),
                                data["has_cookie_banner"], data["has_privacy_policy"],
                                form_from_row(data))


def verdict(score, finding):
    """api.py partner_status() rules."""
    if score is None:
        return None
    if (finding or "").startswith("CMP detected"):
        return "has_cmp"
    if score <= fp.SEVERE_AT:
        return "severe"
    return "moderate" if score < fp.ALIGNED else "clean"


# --- state ---------------------------------------------------------------------
def fetch_targets(conn):
    cur = conn.cursor()
    cur.execute(f"""SELECT Id, CompanyName, Website, {', '.join(OVERWRITE)}, UpdatedAt
                    FROM {TABLE} WHERE Source = %s AND ScrapeStatus = 'done'
                    ORDER BY Id""", (pia.SOURCE,))
    cols = ("Id", "CompanyName", "Website", *OVERWRITE, "UpdatedAt")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def start_or_resume(conn):
    """(backup rows by Id, backup path). Backs up on the first start only."""
    if STATE.exists():
        state = json.loads(STATE.read_text())
        backup = Path(state["backup"])
        rows = json.loads(backup.read_text())["rows"]
        print(f"resuming run started {state['started_at']} -- backup {backup.name}")
        return {r["Id"]: r for r in rows}, backup
    rows = fetch_targets(conn)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup = DATA / f"partner_consent_backup_{stamp}.json"
    backup.write_text(json.dumps({
        "taken_at": datetime.now().astimezone().isoformat(),
        "database": config.CRM_DB_NAME, "table": TABLE, "source": pia.SOURCE,
        "columns": [*OVERWRITE, "UpdatedAt"], "rows": rows,
    }, ensure_ascii=False, default=str))
    STATE.write_text(json.dumps({"started_at": datetime.now().astimezone().isoformat(),
                                 "backup": str(backup), "rules": fp.FINGERPRINTS_VERSION,
                                 "row_count": len(rows)}))
    print(f"backed up {len(rows)} rows -> {backup}")
    return {r["Id"]: r for r in rows}, backup


def load_progress():
    """Latest entry per Id."""
    latest = {}
    if PROGRESS.exists():
        for line in PROGRESS.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                latest[e["id"]] = e
    return latest


# --- the scan ------------------------------------------------------------------
async def scan_in_process(company):
    """--one worker: one browser, one site, the result as JSON on the last line."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        data, err = await s3.scrape_one(browser, company, False)
        await browser.close()
    print(json.dumps({"data": data, "error": err}, ensure_ascii=False, default=str))


def _kill_group(pid):
    """
    SIGTERM first, so the Playwright driver's exit handler takes its Chromium
    with it (Chromium runs in its own process group); SIGKILL if anything is
    still there after a few seconds.
    """
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        if grace:
            import time
            time.sleep(grace)


async def scan(row):
    domain = pia.domain_of(row["Website"])
    if not domain:
        return None, f"no usable domain in Website {row['Website']!r}", 0
    company = {"resolved_domain": domain, "company_name": row["CompanyName"],
               "cin": f"partner:{row['Id']}"}
    err = None
    for attempt in range(1, ATTEMPTS + 1):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-W", "ignore", str(Path(__file__).resolve()),
            "--one", json.dumps(company),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            out, errout = await asyncio.wait_for(proc.communicate(), SITE_TIMEOUT_S)
        except asyncio.TimeoutError:
            await asyncio.to_thread(_kill_group, proc.pid)
            await proc.wait()
            err = f"site did not finish within {SITE_TIMEOUT_S}s"
            continue
        try:
            result = json.loads(out.decode().strip().splitlines()[-1])
        except (IndexError, ValueError):
            tail = errout.decode(errors="replace").strip().splitlines()[-1:] or [""]
            err = f"worker exited {proc.returncode}: {tail[0][:160]}"
            continue
        if result.get("data"):
            return result["data"], None, attempt
        err = result.get("error") or "no data"
    return None, err, ATTEMPTS


def _write_row_direct(row_id, values):
    """--write-row worker: one UPDATE on its own connection, rowcount printed."""
    conn = crm_push.connect()
    cur = conn.cursor()
    sets = ", ".join(f"{c} = %s" for c in OVERWRITE)
    cur.execute(f"UPDATE {TABLE} SET {sets}, UpdatedAt = SYSUTCDATETIME() "
                f"WHERE Id = %s AND Source = %s",
                (*[values[c] for c in OVERWRITE], row_id, pia.SOURCE))
    conn.commit()
    print(cur.rowcount)
    conn.close()


def write_row(row_id, values):
    """
    The overwrite for one row, in a child process under a hard timeout, retried.
    Returns the rowcount; raises RuntimeError when every attempt failed, and
    the caller keeps the row's old values.
    """
    payload = json.dumps({"id": row_id, "values": values}, ensure_ascii=False)
    err = None
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            r = subprocess.run([sys.executable, "-W", "ignore", str(Path(__file__).resolve()),
                                "--write-row", payload],
                               capture_output=True, text=True, timeout=WRITE_TIMEOUT_S)
            if r.returncode == 0:
                return int(r.stdout.strip().splitlines()[-1])
            err = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:160]
        except subprocess.TimeoutExpired:
            err = f"CRM write did not finish within {WRITE_TIMEOUT_S}s"
        if attempt < WRITE_ATTEMPTS:
            time.sleep(10 * attempt)
    raise RuntimeError(err)


async def run(todo, dry_run):
    sem = asyncio.Semaphore(CONCURRENCY)
    done = paused = 0

    async def one(row):
        async with sem:
            if pia.PAUSE_FILE.exists():
                return row, "paused", None, None, 0
            data, err, attempts = await scan(row)
            return row, "ok" if data else "failed", data, err, attempts

    for fut in asyncio.as_completed([one(r) for r in todo]):
        row, outcome, data, err, attempts = await fut
        if outcome == "paused":
            paused += 1
            continue
        entry = {"id": row["Id"], "at": datetime.now().astimezone().isoformat(),
                 "attempts": attempts, "rules": fp.FINGERPRINTS_VERSION}
        if outcome == "ok":
            # Only the six overwrite columns are taken; ContactName is
            # non-empty so values_for() skips the persona titles entirely.
            values = pia.values_for({"ContactName": "-", "CompanyScale": None,
                                     "Industry": None}, data)
            new = {c: values[c] for c in OVERWRITE}
            if not dry_run:
                try:
                    if await asyncio.to_thread(write_row, row["Id"], new) != 1:
                        outcome, err = "failed", "row not found on write"
                except RuntimeError as exc:
                    outcome, err = "failed", f"scanned, but CRM write failed: {exc}"
            if outcome == "ok":
                entry.update(ok=True, new=new, legacy_score=legacy_score(data),
                             scan={k: data.get(k) for k in KEEP})
                tag = f"{row['GapScore']} -> {new['GapScore']}"
        if outcome != "ok":
            entry.update(ok=False, error=err)
            tag = f"FAIL kept old values: {err[:70]}"
        if not dry_run:
            with PROGRESS.open("a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        done += 1
        print(f"  {done:>5}/{len(todo)}  {row['Id']:<6} "
              f"{(pia.domain_of(row['Website']) or '-')[:32]:<32} {tag}", flush=True)
    return paused


def recompute_scan(scan):
    """
    A saved scan re-classified under the current rules -- no page load. The
    form's privacy-link finding has no field of its own and is read back from
    the saved summary, the same reconstruction reclassify_cookies.py uses.
    """
    records = [{**r, "category": fp.cookie_category(r["name"], r["domain"],
                                                     r["party"] == "third")}
               for r in scan["pre_consent_cookies"]]
    a = fp.summarise_records(records)
    banner, policy = scan["has_cookie_banner"], scan["has_privacy_policy"]
    form = form_from_row(scan)
    return {**scan, "pre_consent_cookies": records,
            "tracking_cookie_count": a["tracking_count"],
            "third_party_count": a["third_party_count"],
            "third_party_domains": a["third_party_domains"],
            "max_cookie_lifetime_days": a["max_lifetime_days"],
            "consent_gap_score": fp.consent_score(scan["has_cmp"], a, banner, policy, form),
            "consent_gap_summary": s3.build_consent_summary(
                scan["cmp_name"], a, banner, policy, form, scan["form_embed_provider"])}


def recompute_stale():
    """Rows rescanned under an older rules version, brought up to date offline."""
    stale = [e for e in load_progress().values()
             if e.get("ok") and e.get("rules") != fp.FINGERPRINTS_VERSION]
    if not stale:
        return
    print(f"recomputing {len(stale)} rows scanned under older rules "
          f"({', '.join(sorted({e['rules'] for e in stale}))}) -- no rescan")
    changed = 0
    for e in stale:
        data = recompute_scan(e["scan"])
        values = pia.values_for({"ContactName": "-", "CompanyScale": None,
                                 "Industry": None}, data)
        new = {c: values[c] for c in OVERWRITE}
        if new != e["new"]:
            changed += 1
            try:
                if write_row(e["id"], new) != 1:
                    print(f"  {e['id']}: row not found on write, left as it was")
                    continue
            except RuntimeError as exc:
                print(f"  {e['id']}: CRM write failed ({exc}), left as it was")
                continue
            print(f"  {e['id']:<6} {e['new']['GapScore']} -> {new['GapScore']}")
        with PROGRESS.open("a") as fh:
            fh.write(json.dumps({**e, "at": datetime.now().astimezone().isoformat(),
                                 "rules": fp.FINGERPRINTS_VERSION,
                                 "recomputed_from": e["rules"], "new": new,
                                 "legacy_score": legacy_score(data),
                                 "scan": {k: data.get(k) for k in KEEP}},
                                ensure_ascii=False, default=str) + "\n")
    print(f"{changed} of {len(stale)} recomputed rows changed\n")


# --- report --------------------------------------------------------------------
def report(backup_rows, progress):
    old_c, new_c, moves, why = Counter(), Counter(), Counter(), Counter()
    failed = []
    for rid, old in backup_rows.items():
        vo = verdict(old["GapScore"], old["ConsentFinding"])
        e = progress.get(rid)
        if e and e.get("ok"):
            vn = verdict(e["new"]["GapScore"], e["new"]["ConsentFinding"])
            vl = verdict(e["legacy_score"], e["new"]["ConsentFinding"])
        else:
            vn = vl = vo
            if e:
                failed.append((rid, e.get("error")))
        old_c[vo] += 1
        new_c[vn] += 1
        if vo != vn:
            moves[f"{vo} -> {vn}"] += 1
            if vl == vo:
                why["classification fix only"] += 1
            elif vl == vn:
                why["site/scan changed only (old rules give the same new bucket)"] += 1
            else:
                why["both"] += 1

    not_yet = sum(1 for rid in backup_rows if rid not in progress)
    print(f"\n{'verdict':<10} {'old':>6} {'new':>6}")
    for v in ("severe", "moderate", "clean", "has_cmp"):
        print(f"{v:<10} {old_c[v]:>6} {new_c[v]:>6}")
    print(f"{'total':<10} {sum(old_c.values()):>6} {sum(new_c.values()):>6}")
    print(f"\n{sum(moves.values())} rows changed bucket:")
    for k, n in moves.most_common():
        print(f"  {k:<22} {n}")
    print("why:")
    for k, n in why.most_common():
        print(f"  {k:<62} {n}")
    print(f"\nfailed (old values kept): {len(failed)}")
    errs = Counter((err or "")[:60] for _, err in failed)
    for k, n in errs.most_common(8):
        print(f"  {n:>5}  {k}")
    if not_yet:
        print(f"not yet rescanned: {not_yet}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and print, write nothing (no backup, no progress)")
    ap.add_argument("--limit", type=int, help="rows this invocation, for a trial")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--one", metavar="COMPANY_JSON", help=argparse.SUPPRESS)
    ap.add_argument("--write-row", metavar="ROW_JSON", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.one:
        asyncio.run(scan_in_process(json.loads(args.one)))
        return
    if args.write_row:
        w = json.loads(args.write_row)
        _write_row_direct(w["id"], w["values"])
        return

    # For the one-time reads at start only; every write opens its own
    # short-lived connection in a child process.
    conn = crm_push.connect()
    print(f"target {config.CRM_DB_NAME}.{TABLE}, rules {fp.FINGERPRINTS_VERSION}")

    if args.dry_run:
        todo = fetch_targets(conn)[: args.limit or 6]
        conn.close()
        asyncio.run(run(todo, dry_run=True))
        print("dry run -- nothing written")
        return

    if args.report:
        state = json.loads(STATE.read_text())
        rows = json.loads(Path(state["backup"]).read_text())["rows"]
        report({r["Id"]: r for r in rows}, load_progress())
        return

    if pia.PAUSE_FILE.exists():
        print(f"paused ({pia.PAUSE_FILE.name} present) -- delete it to run")
        return
    backup_rows, backup = start_or_resume(conn)
    conn.close()
    recompute_stale()
    progress = load_progress()
    todo = [r for rid, r in backup_rows.items() if not progress.get(rid, {}).get("ok")]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(backup_rows)} rows in this run, {len(todo)} to scan now\n", flush=True)

    paused = asyncio.run(run(todo, dry_run=False))
    if paused:
        print(f"\nstopped: {pia.PAUSE_FILE.name} appeared, {paused} rows not started. "
              f"Delete it and re-run to continue.")
    report(backup_rows, load_progress())
    print(f"\nbackup: {backup}")


if __name__ == "__main__":
    main()
