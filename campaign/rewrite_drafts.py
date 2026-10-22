"""
One-off: bring every draft that has not been sent up to the current email template and report-link host.

For each draft in the CRM with AE_DraftSentAt NULL (ready or risky, any Source, plus crm.CrmLeads leads): rebuild the email
from the findings stored with the draft (no rescan, no model), with the current template (draft_templates) and the report link
on config.REPORT_PUBLIC_BASE_URL (same key, new host), then write it, per company, in this order:

    1. ae_drafts (subject, bodies, template version) and ae_companies.report_url, in a Postgres transaction;
    2. the CRM, ONE statement through crm_write.update_company(only_if_unsent=True): AE_DraftSubject, AE_DraftBody,
       AE_DraftVersion, AE_DraftGeneratedAt (server time) and, for a dbo.CRM_Scrapper row, DownloadReportUrl;
    3. commit. If the CRM statement matched nothing because the draft was sent in the meantime, the Postgres change is rolled
       back and the company is counted as skipped_sent. A draft that has been sent is never written.

AE_DraftStatus, AE_DraftEmail, AE_DraftBand and the contact are not touched; sent drafts are not read at all.
reports.public_url (the pipeline's own record of the link, read when a report is reused) is kept in step.

--links-only handles the other rows that still carry an old-host DownloadReportUrl: reports with no draft, any Source, legacy
companies included (an explicit, narrow exception granted by Japesh on 2026-10-19: crm_write's legacy_link_only, which allows
DownloadReportUrl and nothing else). It changes only that column (and ae_companies.report_url / reports.public_url), through the
same guarded writer. It skips a test company, a company the CRM already shows an Email activity for, and anything with a sent draft.

    python -m campaign.rewrite_drafts --dry-run [--preview FILE]        # render everything, write nothing
    python -m campaign.rewrite_drafts --ids 25156,16717                  # only these CRM ids (leads: their mapped ids)
    python -m campaign.rewrite_drafts [--limit N]                        # the real run
    python -m campaign.rewrite_drafts --links-only [--dry-run]           # reports with no draft: the link only
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import psycopg2.extras

import config
import db

from . import crm_read, crm_write, draft_templates, flags, locks, settings
from . import settings_drafts as cfg

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "logs" / "rewrite_drafts_records.csv"
LINK_RECORDS = ROOT / "logs" / "rewrite_links_records.csv"
OLD_REPORT_BASE = "https://reports-consiva.gignati.com"
COLUMNS = ["recorded_at", "company_id", "batch", "outcome", "old_report_url", "new_report_url", "error"]
_URL = re.compile(r"^https?://[^/]+/r/([0-9a-f]{32})\.pdf$")


class Skip(Exception):
    def __init__(self, outcome, detail=""):
        super().__init__(detail or outcome)
        self.outcome = outcome


def new_report_url(url):
    """The same report on the current host: the 32-hex key is the object key, so only the base changes."""
    m = _URL.match(url or "")
    if not m:
        raise Skip("skipped_no_report_url", f"not a report url: {url!r}")
    return f"{config.REPORT_PUBLIC_BASE_URL}/r/{m.group(1)}.pdf"


def _facts(findings, company):
    return {"company": findings.get("company_name") or company["company_name"] or company["domain"],
            "n_tracking": findings.get("tracking_cookie_count", 0), "n_third": findings.get("third_party_count", 0),
            "no_banner": not findings.get("cookie_banner", True), "no_policy": not findings.get("privacy_policy", True)}


def build(row):
    """(Rendered, new_url) for one stored draft; Skip when there is nothing true to list or no usable report link."""
    url = new_report_url(row["report_url"])
    try:
        mail = draft_templates.render(row["band"], "", "", "", None, url, row["domain"], _facts(row["findings"], row))
    except draft_templates.NothingToList as exc:
        raise Skip("skipped_nothing_to_list", str(exc))
    return mail, url


def _candidates(reader, ids):
    scope = reader.unsent_drafts()
    wanted = list(scope["companies"])
    if scope["leads"]:
        with db.get_cursor() as cur:
            cur.execute("SELECT company_id FROM ae_lead_map WHERE lead_id = ANY(%s) ORDER BY company_id", (scope["leads"],))
            wanted += [r["company_id"] for r in cur.fetchall()]
    if ids:
        wanted = [i for i in wanted if i in set(ids)]
    with db.get_cursor() as cur:
        cur.execute("""SELECT a.company_id, a.company_name, a.domain, a.report_url, a.report_ref, a.batch, d.band, d.findings, d.draft_status
                       FROM ae_companies a JOIN ae_drafts d USING (company_id)
                       WHERE a.company_id = ANY(%s) AND NOT a.is_test AND a.status = 'written_to_crm'
                         AND d.written_to_crm_at IS NOT NULL AND d.draft_status IN ('ready', 'risky')
                       ORDER BY a.company_id""", (wanted,))
        rows = cur.fetchall()
    return rows, len(wanted)


def _write_one(row, mail, url):
    """Postgres + CRM for one company. Returns 'rewritten' or 'skipped_sent'; raises on failure (nothing kept)."""
    columns = dict(AE_DraftSubject=mail.subject, AE_DraftBody=mail.html,
                   AE_DraftVersion=draft_templates.crm_version(mail.template_version),
                   AE_DraftGeneratedAt=crm_write.SERVER_NOW)
    if row["company_id"] < settings.LEAD_ID_BASE:
        columns["DownloadReportUrl"] = url
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""UPDATE ae_drafts SET subject = %s, body_html = %s, plain_text = %s, template_version = %s,
                               prompt_version = %s, provider = 'fixed', model = 'none', opening_line = '',
                               finding_sentence = '', generated_at = NOW(), written_to_crm_at = NOW()
                           WHERE company_id = %s""",
                        (mail.subject, mail.html, mail.text, mail.template_version, cfg.FIXED_PROMPT_VERSION,
                         row["company_id"]))
            cur.execute("UPDATE ae_companies SET report_url = %s, updated_at = NOW() WHERE company_id = %s",
                        (url, row["company_id"]))
            cur.execute("UPDATE reports SET public_url = %s WHERE id = %s", (url, row.get("report_ref")))
            if not crm_write.update_company(row["company_id"], only_if_unsent=True, allow_held_out=True, **columns):
                conn.rollback()
                return "skipped_sent"
    return "rewritten"


def _link_candidates(reader, ids):
    """(rows to write, ids the CRM lists, ids skipped because the CRM shows an Email activity for them)."""
    listed = reader.old_host_no_draft_ids(OLD_REPORT_BASE)
    if ids:
        listed = [i for i in listed if i in set(ids)]
    emailed = set(reader.emailed_company_ids(listed)) if listed else set()
    with db.get_cursor() as cur:
        cur.execute("""SELECT company_id, report_url, report_ref, batch, status FROM ae_companies
                       WHERE company_id = ANY(%s) AND NOT is_test AND report_url IS NOT NULL
                       ORDER BY company_id""", ([i for i in listed if i not in emailed],))
        rows = cur.fetchall()
    return rows, listed, emailed


def _write_link(row, url):
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("UPDATE ae_companies SET report_url = %s, updated_at = NOW() WHERE company_id = %s",
                        (url, row["company_id"]))
            cur.execute("UPDATE reports SET public_url = %s WHERE id = %s OR (lead_ref = %s AND public_url = %s)",
                        (url, row["report_ref"], row["company_id"], row["report_url"]))
            if not crm_write.update_company(row["company_id"], only_if_unsent=True, legacy_link_only=True,
                                            allow_held_out=True, DownloadReportUrl=url):
                conn.rollback()
                return "skipped_sent"
    return "rewritten"


def run_links(reader=None, ids=None, limit=None, dry_run=False, records=LINK_RECORDS, log=print):
    """Moves the report link of reports that have no draft to the current host. Test companies and already-emailed ones are skipped."""
    own = reader is None
    reader = reader or crm_read.CrmReader()
    try:
        rows, listed, emailed = _link_candidates(reader, ids)
    finally:
        if own:
            reader.close()
    if limit:
        rows = rows[:limit]
    log(f"report links on the old host with no draft: {len(listed)}; to move: {len(rows)}; skipped because the CRM shows an "
        f"Email activity: {len(emailed)}; skipped otherwise: {len(listed) - len(rows) - len(emailed)}"
        f"{' (dry run)' if dry_run else ''}")
    counts, out = Counter(), []
    for row in rows:
        rec = {"recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "company_id": row["company_id"],
               "batch": row["batch"] or "", "old_report_url": row["report_url"], "new_report_url": "", "error": ""}
        try:
            url = new_report_url(row["report_url"])
            rec["new_report_url"] = url
            outcome = "would_rewrite" if dry_run else _write_link(row, url)
        except Skip as exc:
            outcome, rec["error"] = exc.outcome, str(exc)
        except Exception as exc:                       # one company must not stop the run
            outcome, rec["error"] = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
        rec["outcome"] = outcome
        counts[outcome] += 1
        out.append(rec)
    if out and not dry_run:
        _record(records, out)
    log(f"done: {json.dumps(dict(counts), sort_keys=True)}")
    return {"counts": dict(counts), "emailed": len(emailed), "left_alone": len(listed) - len(rows)}


def _record(path, rows):
    path = Path(path)
    path.parent.mkdir(exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerows(rows)


def run(reader=None, ids=None, limit=None, dry_run=False, preview=None, records=RECORDS, log=print):
    own = reader is None
    reader = reader or crm_read.CrmReader()
    try:
        rows, in_crm = _candidates(reader, ids)
    finally:
        if own:
            reader.close()
    if limit:
        rows = rows[:limit]
    counts, out, shown, samples = Counter(), [], set(), []
    log(f"unsent drafts in the CRM: {in_crm}; with a stored draft to rewrite: {len(rows)}"
        f"{' (dry run: nothing will be written)' if dry_run else ''}")
    t0 = time.monotonic()
    for i, row in enumerate(rows, 1):
        rec = {"recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "company_id": row["company_id"],
               "batch": row["batch"] or "", "old_report_url": row["report_url"], "new_report_url": "", "error": ""}
        try:
            mail, url = build(row)
            rec["new_report_url"] = url
            outcome = "would_rewrite" if dry_run else _write_one(row, mail, url)
            if row["band"] not in shown and len(samples) < 4:
                shown.add(row["band"])
                samples.append((row, mail))
        except Skip as exc:
            outcome, rec["error"] = exc.outcome, str(exc)
        except (crm_write.CrmWriteRefused, crm_write.CrmWriteFailed) as exc:
            outcome, rec["error"] = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
        except Exception as exc:                       # one company must not stop the run
            outcome, rec["error"] = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
        rec["outcome"] = outcome
        counts[outcome] += 1
        out.append(rec)
        if len(out) >= 100 or i == len(rows):
            if not dry_run:
                _record(records, out)
            out = []
        if i % 250 == 0:
            log(f"  {i}/{len(rows)} {dict(counts)} ({time.monotonic() - t0:.0f}s)")
    log(f"done in {time.monotonic() - t0:.0f}s: {json.dumps(dict(counts), sort_keys=True)}")
    if preview and samples:
        _write_preview(preview, samples)
        log(f"preview of {len(samples)} sample emails (one per band seen): {preview}")
    return {"counts": dict(counts), "samples": samples}


def _write_preview(path, samples):
    parts = ["<!doctype html><meta charset=utf-8><title>Draft preview</title><body style='font-family:Arial;max-width:680px;margin:24px auto'>"]
    for row, mail in samples:
        parts.append(f"<hr><p style='color:#555'><b>company {row['company_id']}</b> &middot; band {row['band']} &middot; "
                     f"{mail.template_version}<br><b>Subject:</b> {mail.subject}</p>{mail.html}")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="render everything, write nothing")
    ap.add_argument("--ids", type=lambda v: [int(x) for x in v.split(",") if x.strip()], help="only these ids")
    ap.add_argument("--limit", type=int, help="most drafts to rewrite")
    ap.add_argument("--preview", help="write sample emails (one per band) to this HTML file")
    ap.add_argument("--links-only", action="store_true", help="only move the report link of reports that have no draft")
    args = ap.parse_args(argv)
    if flags.kill_switch_on():
        print("kill switch is on: not running")
        return 1
    with locks.job_lock("rewrite_drafts") as got:
        if not got:
            print("another rewrite_drafts run holds the lock")
            return 1
        if args.links_only:
            run_links(ids=args.ids, limit=args.limit, dry_run=args.dry_run)
            return 0
        result = run(ids=args.ids, limit=args.limit, dry_run=args.dry_run, preview=args.preview)
        for row, mail in result["samples"][:1]:
            print(f"\n--- sample: company {row['company_id']} band {row['band']}\nSUBJECT: {mail.subject}\n\n{mail.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
