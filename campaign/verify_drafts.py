"""
One-off: run NeverBounce on the drafts of the held-out express batches (GoodFirms, Clutch) that were written to the CRM without it
(nb_source 'skipped') and nobody has sent yet (AE_DraftSentAt NULL), and move AE_DraftStatus to what the verdict says:

    valid                       stays 'ready'   (contact recorded as nb_source 'live', decision OK)
    catchall / unknown          'risky'         (ae_companies.risky = TRUE)
    invalid / disposable        'expired'       (nobody sends it; the address goes to ae_suppression; company -> expired)

Per company, in this order: the verdict (cache, else one credit), then a Postgres transaction (ae_contacts, ae_drafts, ae_companies)
and, when AE_DraftStatus changes, ONE CRM statement through crm_write.update_company(only_if_unsent=True, allow_held_out=True). If
that matched nothing because the draft was sent in the meantime, the Postgres change is rolled back (the credit is spent, the
verdict stays cached). AE_DraftStatus is the only CRM column written. Credits count against AE_NEVERBOUNCE_DAILY_CREDIT_CAP; the run
stops when the cap or the account balance is used up and a rerun carries on (verified drafts are no longer 'skipped').

    python -m campaign.verify_drafts --dry-run                  # count what would be asked, spend nothing, write nothing
    python -m campaign.verify_drafts --limit 50 [--batch goodfirms]
    python -m campaign.verify_drafts                            # everything unsent, up to the daily cap

Needs AE_NB_MODE=live and NEVERBOUNCE_API_KEY (a mock verdict is never used here).
"""
import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import psycopg2.extras

import db

from . import crm_read, crm_write, draft_templates, flags, locks, neverbounce, settings, settings_verify, status, verify

CHUNK = 200


def _candidates(unsent_ids, batch=None):
    with db.get_cursor() as cur:
        cur.execute("""SELECT a.company_id, a.batch, ct.contact_id, LOWER(TRIM(ct.email)) AS email, d.draft_status
                       FROM ae_companies a JOIN ae_drafts d USING (company_id)
                       JOIN ae_contacts ct ON ct.contact_id = a.chosen_contact_id
                       WHERE a.company_id = ANY(%s) AND NOT a.is_test AND a.status = 'written_to_crm'
                         AND a.batch = ANY(%s) AND (%s::text IS NULL OR a.batch = %s)
                         AND d.draft_status IN ('ready', 'risky') AND d.written_to_crm_at IS NOT NULL
                         AND ct.nb_source = 'skipped'
                       ORDER BY a.company_id""", (list(unsent_ids), list(settings.EXPRESS_BATCHES), batch, batch))
        return cur.fetchall()


def _apply(row, verdict):
    """Postgres + CRM for one company. Returns 'ready' / 'risky' / 'expired' or 'skipped_sent'."""
    decision = verify.DECISION[verdict]
    new = {"OK": "ready", "RISKY": "risky", "DO_NOT_EMAIL": "expired"}[decision]
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status FROM ae_companies WHERE company_id = %s FOR UPDATE", (row["company_id"],))
            if cur.fetchone()["status"] != "written_to_crm":
                return "skipped_moved"
            verify._mark(cur, {"contact_id": row["contact_id"]}, verdict, decision, "live")
            cur.execute("UPDATE ae_companies SET risky = %s, updated_at = NOW() WHERE company_id = %s",
                        (decision == "RISKY", row["company_id"]))
            cur.execute("UPDATE ae_drafts SET draft_status = %s WHERE company_id = %s", (new, row["company_id"]))
            if decision == "DO_NOT_EMAIL":
                status.set_status(cur, row["company_id"], "expired", f"NeverBounce (live): {verdict}; draft expired")
            if new != row["draft_status"]:
                columns = {"AE_DraftStatus": new}
                if new != "expired":                       # crm_write wants the template version with ready / risky
                    cur.execute("SELECT template_version FROM ae_drafts WHERE company_id = %s", (row["company_id"],))
                    columns["AE_DraftVersion"] = draft_templates.crm_version(cur.fetchone()["template_version"])
                if not crm_write.update_company(row["company_id"], only_if_unsent=True, allow_held_out=True, **columns):
                    conn.rollback()
                    return "skipped_sent"
    if decision == "DO_NOT_EMAIL":
        verify._suppress(row["email"])
    return new


def run(batch=None, limit=None, dry_run=False, reader=None, client=None, log=print):
    if not dry_run and settings_verify.MODE != "live":
        log("AE_NB_MODE is not 'live': nothing done"); return {"stopped": "not_live"}
    own = reader is None
    reader = reader or crm_read.CrmReader()
    try:
        unsent = reader.unsent_drafts()["companies"]
    finally:
        if own:
            reader.close()
    rows = _candidates(unsent, batch)
    if limit:
        rows = rows[:limit]
    day, counts = verify.today_ist(), Counter()
    with db.get_cursor() as cur:
        cached = verify._cached(cur, [r["email"] for r in rows])
    need = [r for r in rows if cached.get(r["email"], (None, None))[1] != "live"]
    log(f"unsent drafts in the CRM: {len(unsent)}; express-batch drafts never verified: {len(rows)}; "
        f"to ask NeverBounce: {len(need)} (credits used today: {verify._used_today(day)} of {settings_verify.DAILY_CREDIT_CAP})"
        f"{' (dry run)' if dry_run else ''}")
    if dry_run:
        return {"candidates": len(rows), "credits_needed": len({r['email'] for r in need})}
    client = client or neverbounce.make_client("live")
    stopped, balance = None, client.credits_remaining()
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        with db.get_cursor() as cur:
            cache = verify._cached(cur, [r["email"] for r in chunk])
        asks, seen = [], set()
        for r in chunk:
            if cache.get(r["email"], (None, None))[1] == "live" or r["email"] in seen:
                continue
            if (balance is not None and balance - len(asks) < 1) or not verify._reserve_credit(day, settings_verify.DAILY_CREDIT_CAP):
                stopped = "low_balance" if (balance is not None and balance - len(asks) < 1) else "daily_cap"
                break
            seen.add(r["email"]); asks.append(r["email"])

        def call(email):
            try:
                return email, client.check(email), None
            except (neverbounce.TransientError, neverbounce.NeverBounceError) as exc:
                return email, None, exc
        failed = set()
        with ThreadPoolExecutor(max_workers=settings_verify.CONCURRENCY) as pool:
            for email, verdict, exc in pool.map(call, asks):
                if exc is not None:
                    verify._release_credit(day); failed.add(email)
                    log(f"  NeverBounce: {email.split('@')[-1]}: {type(exc).__name__}: {exc}")
                    continue
                counts["credits_used"] += 1
                if balance is not None:
                    balance -= 1
                verify._cache_verdict(email, verdict, "live")
        if asks and len(failed) == len(asks):
            stopped = stopped or "api_error"                # every call of the chunk failed: stop, a rerun retries
        with db.get_cursor() as cur:
            cache = verify._cached(cur, [r["email"] for r in chunk])
        for r in chunk:
            hit = cache.get(r["email"])
            if not hit or hit[1] != "live":
                counts["left_for_next_run"] += 1
                continue
            try:
                counts[_apply(r, hit[0])] += 1
            except Exception as exc:                       # one company must not stop the run
                counts["failed"] += 1
                log(f"  {r['company_id']}: {type(exc).__name__}: {str(exc)[:160]}")
        log(f"  {min(i + CHUNK, len(rows))}/{len(rows)} {dict(counts)}")
        if stopped:
            break
    log(f"done: {dict(counts)}{' stopped: ' + stopped if stopped else ''}")
    return {"counts": dict(counts), "stopped": stopped}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--batch", choices=list(settings.EXPRESS_BATCHES))
    args = ap.parse_args(argv)
    if flags.kill_switch_on():
        print("kill switch is on: not running")
        return 1
    with locks.job_lock("verify_drafts") as got:
        if not got:
            print("another verify_drafts run holds the lock")
            return 1
        run(batch=args.batch, limit=args.limit, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
