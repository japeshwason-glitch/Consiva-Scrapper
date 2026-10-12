"""
One-page DPDP Consent Readiness Report for every PartnerImport lead marked
done in dbo.CRM_Scrapper: scan the homepage, compute findings, render a
one-page A4 PDF, upload it to R2 and write its public URL back to the lead.
Full flow, config and bucket/URL mapping: docs/REPORTS.md.

    python report_build.py --preflight                  # checks only
    python report_build.py --dry-run --limit 1          # local PDF only
    python report_build.py --dry-run --sample-bands     # one lead per score bucket + one G5
    python report_build.py --ids-from-last-sample       # those same leads, for real
    python report_build.py --id 6570                    # one lead, for real
    python report_build.py --retry-failed               # leads whose ReportStatus is failed
    python report_build.py [--limit N]                  # the full run (resumable)

Run it in the reports container (docker compose --profile reports run --rm
reports python report_build.py ...); the pipeline image lacks its libraries.

Ground rules this file keeps:
  * No model anywhere. Findings, wording, summary and score are
    report_findings.py over the scan; the template is fixed.
  * On SQL Server it only UPDATEs DownloadReportUrl, ReportStatus and
    ReportGeneratedAt, and only on PartnerImport rows marked done.
  * Postgres access goes through db.py, into the report tables only.
  * Resumable: a lead that already has a rendered report for the current
    template and catalogue versions is skipped (or only re-pushed to the CRM
    if that write never landed). data/reports.pause stops it cleanly.

--dry-run scans, saves the scan and findings, and renders PDFs to
data/reports_preview/. It uploads nothing, writes no `reports` row and never
touches the CRM.
"""

# RETIRED. PartnerImport leads are handled by the campaign pipeline (campaign/), which
# owns ScrapeStatus and ReportStatus for them. This guard runs before any import, so nothing
# below is loaded and nothing is connected to. Importing this module (the tests do) is fine;
# only running it as a script is refused.
if __name__ == "__main__":
    raise SystemExit("report_build.py is retired: PartnerImport leads are now handled by the "
                     "campaign pipeline (python -m campaign.jobs). Refusing to run.")
import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config
import crm_push
import db
import fingerprints as fp
import partner_import_analyse as pia
import report_findings as rf

s3 = pia._stage03

TEMPLATE_PATH = ROOT / "templates" / "report_one_page.html.j2"
LOGO_PATH = ROOT / "assets" / "consiva-nav-logo.png"
FONT_DIR = ROOT / "assets" / "fonts"
FONTS = (  # (family, weight, file)
    ("IBM Plex Sans", 400, "IBMPlexSans-Regular.woff2"),
    ("IBM Plex Sans", 500, "IBMPlexSans-Medium.woff2"),
    ("IBM Plex Sans", 600, "IBMPlexSans-SemiBold.woff2"),
    ("IBM Plex Sans", 700, "IBMPlexSans-Bold.woff2"),
    ("IBM Plex Mono", 400, "IBMPlexMono-Regular.woff2"),
    ("IBM Plex Mono", 500, "IBMPlexMono-Medium.woff2"),
)

DATA = ROOT / "data"
PREVIEW_DIR = DATA / "reports_preview"
SAMPLE_FILE = DATA / "last_sample_ids.json"
PAUSE_FILE = DATA / "reports.pause"

CONCURRENCY = 6
SCAN_ATTEMPTS = 3            # 1 + 2 retries
SCAN_TIMEOUT_S = 150         # one homepage, all waits included -- up to four URLs
                             # (https, http, then www.) when the bare domain fails
RENDER_TIMEOUT_S = 60
CRM_WRITE_TIMEOUT_S = 45
CRM_WRITE_ATTEMPTS = 3
TABLE = config.CRM_DB_TABLE
REQUIRED_COLUMNS = {"DownloadReportUrl": 200, "ReportStatus": 20, "ReportGeneratedAt": None}
SAMPLE_BUCKETS = (("[0,25)", 0, 25), ("[25,30)", 25, 30), ("[30,60)", 30, 60),
                  ("[60,90)", 60, 90), ("[90,100)", 90, 100), ("=100 (no gaps)", 100, 101))


def _sha(*paths, n=12):
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).read_bytes())
    return h.hexdigest()[:n]


# Versions are content hashes: editing the template or the scanner changes
# them automatically, and a template change makes every report regenerate.
TEMPLATE_VERSION = "t-" + _sha(TEMPLATE_PATH)
SCANNER_SHA = _sha(ROOT / "stages" / "03_scrape_playwright.py", ROOT / "fingerprints.py")


# --- pre-flight ----------------------------------------------------------------
class PreflightError(SystemExit):
    pass


def check_crm_columns(conn):
    schema, table = TABLE.split(".")
    cur = conn.cursor()
    cur.execute("""SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
                   FROM INFORMATION_SCHEMA.COLUMNS
                   WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s""", (schema, table))
    cols = {name: (dtype, length) for name, dtype, length in cur.fetchall()}
    problems = []
    for want, min_len in REQUIRED_COLUMNS.items():
        hit = cols.get(want)
        near = [c for c in cols if c.strip().lower() == want.lower() and c != want]
        if not hit:
            problems.append(f"{want} missing" + (f" (found {near[0]!r} -- note the exact spelling)"
                                                 if near else ""))
            continue
        dtype, length = hit
        print(f"  [{want}]  {dtype}({'max' if length == -1 else length})")
        if min_len and length != -1 and (length or 0) < min_len:
            problems.append(f"{want} is {dtype}({length}); needs at least nvarchar({min_len})")
    if problems:
        raise PreflightError("STOP -- CRM columns not ready: " + "; ".join(problems))


def check_r2():
    import urllib.request
    import storage
    key, body = "healthcheck/ok.txt", f"ok {datetime.now().isoformat()}"
    storage.put_text(key, body)
    try:
        url = storage.public_url(key)
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                                   "User-Agent": "consiva-report-preflight"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            got, status = resp.read().decode(), resp.status
    except Exception as exc:
        raise PreflightError(f"STOP -- uploaded to R2 but could not fetch {url}: {exc}")
    finally:
        storage.delete(key)
    if status != 200 or got != body:
        raise PreflightError(f"STOP -- {url} returned {status} with different content")
    print(f"  R2 upload -> {url} -> HTTP 200, same content -> deleted")


def check_fonts():
    missing = [f for _, _, f in FONTS if not (FONT_DIR / f).exists()]
    if missing:
        raise PreflightError(f"STOP -- fonts missing in assets/fonts/: {', '.join(missing)}")
    print(f"  fonts: {len(FONTS)} woff2 files present")


def preflight(conn, r2=True):
    print("pre-flight:")
    check_crm_columns(conn)
    check_fonts()
    if r2:
        check_r2()
    print(f"  template {TEMPLATE_VERSION}, catalogue {rf.CATALOGUE_VERSION}, "
          f"scanner {SCANNER_SHA}, rules {fp.FINGERPRINTS_VERSION}\n")


# --- CRM ------------------------------------------------------------------------
# The consent findings this job writes back from its own scan -- built exactly
# as the partner job and the partner rescan build them (values_for()).
CRM_SCAN_COLUMNS = ("GapScore", "TrackersPreConsent", "ThirdPartiesCount",
                    "CookieBanner", "CollectsViaForm", "ConsentFinding")
# Every SQL Server column this job may write. Nothing else, ever. UpdatedAt
# is stamped only together with the six finding columns.
CRM_WRITE_COLUMNS = CRM_SCAN_COLUMNS + ("DownloadReportUrl", "ReportStatus",
                                        "ReportGeneratedAt", "UpdatedAt")
# Only a scan that completed and was read (a report, or nothing to report)
# replaces the six findings. An unscannable or failed scan measured a wall, a
# parked page or nothing at all, and must never overwrite a real finding.
FINDINGS_WRITTEN_FOR = ("generated", "no_gap")

LEAD_COLS = ("Id", "CompanyName", "Website", *CRM_SCAN_COLUMNS,
             "DownloadReportUrl", "ReportStatus")


def fetch_leads(conn):
    cur = conn.cursor()
    cur.execute(f"""SELECT {', '.join(LEAD_COLS)} FROM {TABLE}
                    WHERE Source = %s AND ScrapeStatus = 'done' ORDER BY Id""",
                (pia.SOURCE,))
    return [dict(zip(LEAD_COLS, r)) for r in cur.fetchall()]


def crm_write_plan(lead_id, status, url=None, set_url=False, values=None):
    """
    What one lead's CRM write carries. The six finding columns travel only
    with a status in FINDINGS_WRITTEN_FOR; for unscannable / failed the write
    is ReportStatus and ReportGeneratedAt alone, whatever the caller passed.
    """
    return {"id": lead_id, "status": status, "url": url, "set_url": set_url,
            "values": values if status in FINDINGS_WRITTEN_FOR else None}


def crm_update_sql(w):
    """
    (sql, params) for one write plan: ReportStatus and ReportGeneratedAt
    always, DownloadReportUrl when set_url, the six finding columns plus
    UpdatedAt when values. The only UPDATE this job sends; every column it
    names is checked against CRM_WRITE_COLUMNS, and it only ever matches a
    PartnerImport row marked done.
    """
    cols, params = ["ReportStatus"], [w["status"]]
    if w.get("set_url"):
        cols.append("DownloadReportUrl")
        params.append(w.get("url"))
    for c, v in (w.get("values") or {}).items():
        cols.append(c)
        params.append(v)
    stamps = ["ReportGeneratedAt"] + (["UpdatedAt"] if w.get("values") else [])
    unknown = set(cols + stamps) - set(CRM_WRITE_COLUMNS)
    if unknown:
        raise ValueError(f"refusing to write CRM columns outside the allowed ten: {unknown}")
    sets = [f"{c} = %s" for c in cols] + [f"{c} = SYSUTCDATETIME()" for c in stamps]
    sql = (f"UPDATE {TABLE} SET {', '.join(sets)} "
           f"WHERE Id = %s AND Source = %s AND ScrapeStatus = 'done'")
    return sql, (*params, w["id"], pia.SOURCE)


def _crm_write_direct(w):
    """--crm-write worker: one UPDATE (crm_update_sql), rowcount printed."""
    conn = crm_push.connect()
    cur = conn.cursor()
    cur.execute(*crm_update_sql(w))
    conn.commit()
    print(cur.rowcount)
    conn.close()


def crm_write(lead_id, status, url=None, set_url=False, values=None):
    """
    One lead's CRM write (crm_write_plan -> crm_update_sql), in a child
    process under a hard timeout -- a pymssql call on a dropped connection can
    block forever. Returns the rowcount; raises RuntimeError if every attempt
    failed.
    """
    w = crm_write_plan(lead_id, status, url, set_url, values)
    crm_update_sql(w)                       # refuse a bad column before spawning anything
    payload = json.dumps(w, ensure_ascii=False)
    err = None
    for attempt in range(1, CRM_WRITE_ATTEMPTS + 1):
        try:
            r = subprocess.run([sys.executable, "-W", "ignore", str(Path(__file__).resolve()),
                                "--crm-write", payload],
                               capture_output=True, text=True, timeout=CRM_WRITE_TIMEOUT_S)
            if r.returncode == 0:
                return int(r.stdout.strip().splitlines()[-1])
            err = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:160]
        except subprocess.TimeoutExpired:
            err = f"CRM write did not finish within {CRM_WRITE_TIMEOUT_S}s"
        if attempt < CRM_WRITE_ATTEMPTS:
            time.sleep(10 * attempt)
    raise RuntimeError(err)


def crm_values(data):
    """
    The six CRM consent columns from one scrape_one() result -- the partner
    job's own values_for(), so they are byte-identical to what the partner
    rescan wrote. ContactName is non-empty only so values_for() skips the
    persona titles, which are not among the columns written.
    """
    values = pia.values_for({"ContactName": "-", "CompanyScale": None, "Industry": None}, data)
    return {c: values[c] for c in CRM_SCAN_COLUMNS}


def backup_crm(leads):
    """The six scan columns of every row this run may touch, before any write."""
    path = DATA / f"crm_score_backup_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.write_text(json.dumps({
        "taken_at": datetime.now().astimezone().isoformat(),
        "database": config.CRM_DB_NAME, "table": TABLE, "columns": list(CRM_SCAN_COLUMNS),
        "rows": [{"Id": l["Id"], **{c: l[c] for c in CRM_SCAN_COLUMNS}} for l in leads],
    }, ensure_ascii=False, default=str, indent=1))
    return path


# --- choosing leads ---------------------------------------------------------------
_AD_OR_REPLAY = fp.ADVERTISING_COOKIE_PREFIXES + fp.SESSION_REPLAY_COOKIE_PREFIXES


def _finding_cookie_names(finding):
    m = re.search(r"tracking cookies set before consent: ([^;]+)", finding or "")
    return [n.strip() for n in re.sub(r"\(\+\d+ more\)", "", m.group(1)).split(",")] if m else []


def likely_g5(lead):
    return (lead["ConsentFinding"] or "").startswith("CMP detected") \
        and (lead["TrackersPreConsent"] or 0) > 0


# Cookie names only an embedded YouTube player sets.
_YOUTUBE_NAMES = {n for n, v in fp.VENDOR_BY_EXACT.items() if v == "YouTube"}


def likely_youtube_only(lead):
    """
    The CRM finding lists every tracking cookie (no "(+N more)") and each is a
    YouTube embed cookie. Those leads go last: their only finding is the one a
    recipient most easily waves away.
    """
    finding = lead["ConsentFinding"] or ""
    names = _finding_cookie_names(finding)
    return bool(names) and "more)" not in finding and set(names) <= _YOUTUBE_NAMES


def run_order(lead):
    """G5 first, then G1 with advertising/session-replay vendors, then other
    G1, then the rest, then YouTube-only leads; ties by lowest score. From the
    CRM's last scan."""
    trackers = (lead["TrackersPreConsent"] or 0) > 0
    if likely_youtube_only(lead):
        group = 4
    elif likely_g5(lead):
        group = 0
    elif trackers and any(n.startswith(_AD_OR_REPLAY)
                          for n in _finding_cookie_names(lead["ConsentFinding"])):
        group = 1
    elif trackers:
        group = 2
    else:
        group = 3
    score = lead["GapScore"] if lead["GapScore"] is not None else 101
    return (group, score, lead["Id"])


def pick_sample(leads):
    """One lead per CRM GapScore bucket, random within it, plus one likely G5."""
    rng = random.Random()
    picks = []
    for label, lo, hi in SAMPLE_BUCKETS:
        pool = [l for l in leads if l["GapScore"] is not None and lo <= l["GapScore"] < hi
                and not (hi == 101 and (l["ConsentFinding"] or "").startswith("CMP detected"))]
        if pool:
            picks.append((label, rng.choice(pool)))
        else:
            print(f"  bucket {label}: no leads, skipped")
    chosen = {l["Id"] for _, l in picks}
    g5_pool = [l for l in leads if likely_g5(l) and l["Id"] not in chosen]
    if g5_pool:
        picks.append(("G5 (CMP + trackers)", rng.choice(g5_pool)))
    else:
        print("  G5: no lead whose last scan had a CMP with trackers firing")
    SAMPLE_FILE.write_text(json.dumps(
        {"picked_at": datetime.now().astimezone().isoformat(),
         "leads": [{"id": l["Id"], "bucket": b} for b, l in picks]}, indent=1))
    return picks


# --- scanning ---------------------------------------------------------------------
async def _scan_worker(company):
    """--scan-one worker: one browser, one homepage, the result as JSON."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            data, err = await s3.scrape_one(browser, company, False, report_mode=True)
        finally:
            await browser.close()
    print(json.dumps({"data": data, "error": err}, ensure_ascii=False, default=str))


def _kill_group(pid):
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        time.sleep(grace)


async def scan(lead, domain):
    """(data | None, error | None, attempts) -- each attempt in a killable process."""
    company = {"resolved_domain": domain, "company_name": lead["CompanyName"],
               "cin": f"partner:{lead['Id']}"}
    err = None
    for attempt in range(1, SCAN_ATTEMPTS + 1):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-W", "ignore", str(Path(__file__).resolve()),
            "--scan-one", json.dumps(company),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            out, errout = await asyncio.wait_for(proc.communicate(), SCAN_TIMEOUT_S)
        except asyncio.TimeoutError:
            await asyncio.to_thread(_kill_group, proc.pid)
            await proc.wait()
            err = f"homepage did not finish within {SCAN_TIMEOUT_S}s"
            continue
        try:
            result = json.loads(out.decode().strip().splitlines()[-1])
        except (IndexError, ValueError):
            tail = errout.decode(errors="replace").strip().splitlines()[-1:] or [""]
            err = f"scan worker exited {proc.returncode}: {tail[0][:160]}"
            continue
        if result.get("data"):
            return result["data"], None, attempt
        err = result.get("error") or "no data"
    return None, err, SCAN_ATTEMPTS


def scan_row(lead, domain, data, err, attempts):
    """The report_scans row for one scan (findings and score added later)."""
    row = {"lead_ref": lead["Id"], "company_name": lead["CompanyName"],
           "requested_domain": domain, "scanner_sha": SCANNER_SHA,
           "fingerprints_version": fp.FINGERPRINTS_VERSION, "attempts": attempts}
    if not data:
        return {**row, "scan_status": "failed", "error": (err or "no data")[:1000]}
    return {**row,
            "final_url": data.get("final_url"),
            "final_domain": data.get("final_domain"),
            "http_status": data.get("http_status"),
            "scan_status": data.get("scan_status") or "ok",
            "unscannable_reason": data.get("unscannable_reason"),
            "has_cmp": data.get("has_cmp"),
            "cmp_name": data.get("cmp_name"),
            "cmp_evidence": data.get("cmp_evidence"),
            "has_cookie_banner": data.get("has_cookie_banner"),
            "has_privacy_policy": data.get("has_privacy_policy"),
            "cookies": data.get("pre_consent_cookies") or [],
            "tracking_cookie_count": data.get("tracking_cookie_count"),
            "third_party_tracking_domains": data.get("third_party_tracking_domains") or [],
            "has_data_form": data.get("has_data_form"),
            "form_field_types": data.get("form_field_types") or [],
            "form_consent_checkbox": data.get("form_consent_checkbox"),
            "form_privacy_link": data.get("form_privacy_link"),
            "form_third_party_hosts": data.get("form_third_party_hosts") or []}


# --- rendering ---------------------------------------------------------------------
def font_css():
    """
    @font-face rules with the fonts inlined as base64, so rendering needs no
    network. Deliberately free of quotes, & and <>: the template prints this
    with autoescape on and no |safe, and an escaped quote (&#39;) silently
    breaks the rule -- Chromium then falls back to a system font. Family names
    are unquoted identifiers and the data URI needs no quotes.
    """
    rules = []
    for family, weight, name in FONTS:
        b64 = base64.b64encode((FONT_DIR / name).read_bytes()).decode()
        rules.append(f"@font-face {{ font-family: {family}; font-style: normal; "
                     f"font-weight: {weight}; font-display: block; "
                     f"src: url(data:font/woff2;base64,{b64}); }}")
    css = "\n".join(rules)
    assert not set(css) & set("&<>\"'"), "font_css must survive autoescape unchanged"
    return css


def _jinja_env():
    import jinja2
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=True,                     # the .j2 suffix would defeat select_autoescape
        undefined=jinja2.StrictUndefined)    # a missing context key is an error, not a blank


_STATIC = {}


def display_domain(domain):
    """The name on the report: bare lowercase host -- no scheme, www. or slash."""
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", (domain or "").strip().lower())
    return d.split("/")[0].removeprefix("www.")


def render_html(domain, scan_date, result):
    if not _STATIC:
        _STATIC["env"] = _jinja_env()
        _STATIC["font_css"] = font_css()
        _STATIC["logo"] = "data:image/png;base64," + base64.b64encode(LOGO_PATH.read_bytes()).decode()
    return _STATIC["env"].get_template(TEMPLATE_PATH.name).render(
        font_css=_STATIC["font_css"], logo_data_uri=_STATIC["logo"],
        domain=display_domain(domain), scan_date=scan_date,
        score=result["score"], band=result["band"], band_label=result["band_label"],
        score_line=result["score_line"], review_label=result["review_label"],
        cookie_list=result["cookie_list"], note=result["note"],
        summary=result["summary"], gaps=result["gaps"], reviews=result["reviews"],
        helps=rf.helps(), sales_contact=config.REPORT_SALES_CONTACT)


OVERFLOW = "content overflows the page"
OVERFLOW_JS = """() => { const p = document.querySelector('.page');
  return p.scrollHeight > p.clientHeight + 1 || p.scrollWidth > p.clientWidth + 1; }"""


async def render_pdf(browser, html):
    """(pdf bytes, None) or (None, reason). Exactly one page, nothing clipped."""
    from pypdf import PdfReader
    page = await browser.new_page()
    try:
        await page.set_content(html, wait_until="load")
        await page.evaluate("document.fonts.ready.then(() => true)")
        # .page is a fixed A4 box with overflow:hidden, so a PDF is always one
        # page even when content spills -- the page count alone cannot catch
        # clipping. Measure it.
        if await page.evaluate(OVERFLOW_JS):
            return None, OVERFLOW
        pdf = await page.pdf(format="A4", print_background=True)
    finally:
        await page.close()
    pages = len(PdfReader(io.BytesIO(pdf)).pages)
    if pages != 1:
        return None, f"PDF has {pages} pages"
    return pdf, None


# How a report is made to fit one page, tried in order; gaps are never dropped.
# (keep the review row, report_findings.COMPACT_LEVELS index)
FIT_STEPS = ((True, 0), (False, 0), (False, 1), (False, 2), (False, 3))


def fit_label(keep_review, level):
    parts = ([] if keep_review else ["review omitted"]) + ([f"compact {level}"] if level else [])
    return ", ".join(parts) or "full"


async def render_fitted(browser, domain, scan_date, scan):
    """
    (pdf, reason, fit). Renders at full detail; only while the page overflows
    does it drop the review row, then step cookie detail down through
    report_findings.COMPACT_LEVELS. Every gap stays on the page.
    """
    why = None
    for keep_review, level in FIT_STEPS:
        result = rf.evaluate(scan, compact=level)
        if not keep_review:
            result = {**result, "reviews": [], "review_count": 0}
        pdf, why = await render_pdf(browser, render_html(domain, scan_date, result))
        if pdf or why != OVERFLOW:
            return pdf, why, fit_label(keep_review, level)
    return None, f"{why} even with the review omitted at the most compact level", None


def scan_date_text(when=None):
    when = when or datetime.now().astimezone()
    return f"{when.day} {when:%B %Y}"


def preview_name(lead, domain):
    return f"{lead['Id']}-{re.sub(r'[^A-Za-z0-9.-]', '-', domain or 'site')}.pdf"


# --- one lead ------------------------------------------------------------------------
async def process(lead, browser, render_sem, dry_run):
    """Returns the outcome dict printed and written to the run CSV."""
    out = {"id": lead["Id"], "company": lead["CompanyName"], "crm_score": lead["GapScore"],
           "domain": pia.domain_of(lead["Website"]), "status": None, "score": None,
           "codes": [], "url": None, "band": None, "error": None, "fit": None,
           "crm_values": None}
    domain = out["domain"]
    if not domain:
        data, err, attempts = None, f"no usable domain in Website {lead['Website']!r}", 0
    else:
        data, err, attempts = await scan(lead, domain)
    row = scan_row(lead, domain, data, err, attempts)
    # The six consent columns this scan would write back, so the CRM and the
    # PDF come from the same scan. crm_write_plan() drops them for an
    # unscannable or failed scan.
    values = crm_values(data) if data and row["scan_status"] == "ok" else None
    if values:
        row["crm_values"] = values
        out["crm_values"] = values

    result = rf.evaluate(row) if row["scan_status"] == "ok" else None
    if result:
        row["readiness_score"] = result["score"]
        out.update(score=result["score"], band=result["band"],
                   codes=[f["code"] for f in result["findings"]])
        out["domain"] = row.get("final_domain") or domain
        if result["score"] != values["GapScore"]:
            # One source of truth: never print a score the CRM would not hold.
            out["error"] = (f"report score {result['score']} != CRM GapScore "
                            f"{values['GapScore']} for the same scan")
            values = None
            row["scan_status"], row["error"] = "failed", out["error"]
            result = None
    scan_id = await asyncio.to_thread(db.insert_report_scan, **row)
    if result:
        await asyncio.to_thread(db.insert_report_findings, scan_id, result["findings"],
                                rf.CATALOGUE_VERSION)

    async def finish(status, url=None, set_url=False):
        out["status"] = status
        if not dry_run:
            await asyncio.to_thread(crm_write, lead["Id"], status, url, set_url, values)
        return out

    if row["scan_status"] != "ok":
        out["error"] = out["error"] or row.get("unscannable_reason") or row.get("error")
        return await finish(row["scan_status"])
    if not result["gaps"]:
        # A fresh scan with nothing to report: clear any older report link.
        return await finish("no_gap", None, set_url=True)

    async with render_sem:
        try:
            pdf, why, out["fit"] = await asyncio.wait_for(
                render_fitted(browser, out["domain"], scan_date_text(), row),
                RENDER_TIMEOUT_S)
        except asyncio.TimeoutError:
            pdf, why = None, f"render did not finish within {RENDER_TIMEOUT_S}s"
    if not pdf:
        out["error"] = why
        if not dry_run:
            await asyncio.to_thread(db.insert_report, scan_id=scan_id, lead_ref=lead["Id"],
                                    status="failed", template_version=TEMPLATE_VERSION,
                                    catalogue_version=rf.CATALOGUE_VERSION, error=why)
        return await finish("failed")

    if dry_run:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        path = PREVIEW_DIR / preview_name(lead, out["domain"])
        path.write_bytes(pdf)
        out.update(status="rendered (dry run)", url=str(path.relative_to(ROOT)))
        return out

    import storage
    key, url = await asyncio.to_thread(storage.upload_report, pdf, out["domain"])
    report_id = await asyncio.to_thread(
        db.insert_report, scan_id=scan_id, lead_ref=lead["Id"], status="rendered",
        template_version=TEMPLATE_VERSION, catalogue_version=rf.CATALOGUE_VERSION,
        object_key=key, public_url=url, pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        size_bytes=len(pdf))
    out["url"] = url
    await finish("generated", url, set_url=True)
    await asyncio.to_thread(db.mark_report_pushed, report_id)
    return out


async def repush(lead, report):
    """A rendered report whose CRM write never landed: write it, no rescan."""
    await asyncio.to_thread(crm_write, lead["Id"], "generated", report["public_url"], True,
                            report.get("crm_values"))
    await asyncio.to_thread(db.mark_report_pushed, report["id"])
    return {"id": lead["Id"], "company": lead["CompanyName"], "crm_score": lead["GapScore"],
            "domain": pia.domain_of(lead["Website"]), "status": "generated (re-pushed)",
            "score": None, "codes": [], "url": report["public_url"], "band": None, "error": None,
            "fit": None, "crm_values": report.get("crm_values")}


async def run(leads, dry_run, repush_only):
    from playwright.async_api import async_playwright
    sem, render_sem = asyncio.Semaphore(CONCURRENCY), asyncio.Semaphore(2)
    outcomes, paused = [], 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def one(lead):
            async with sem:
                if PAUSE_FILE.exists():
                    return None
                try:
                    if lead["Id"] in repush_only:
                        return await repush(lead, repush_only[lead["Id"]])
                    return await process(lead, browser, render_sem, dry_run)
                except Exception as exc:
                    return {"id": lead["Id"], "company": lead["CompanyName"],
                            "crm_score": lead["GapScore"], "domain": pia.domain_of(lead["Website"]),
                            "status": "error", "score": None, "codes": [], "url": None,
                            "band": None, "error": f"{type(exc).__name__}: {exc}"[:300],
                            "fit": None, "crm_values": None}

        for fut in asyncio.as_completed([one(l) for l in leads]):
            o = await fut
            if o is None:
                paused += 1
                continue
            outcomes.append(o)
            print(f"  {len(outcomes):>5}/{len(leads)}  {o['id']:<6} {(o['domain'] or '-')[:30]:<30} "
                  f"{o['status']:<20} score {o['score'] if o['score'] is not None else '-':<4} "
                  f"{' '.join(o['codes']) or '-':<16} {o['url'] or o['error'] or ''}", flush=True)
        await browser.close()
    return outcomes, paused


# --- reporting ---------------------------------------------------------------------
def summarise(outcomes, picks=None):
    by_status = Counter(o["status"] for o in outcomes)
    by_band = Counter(o["band"] for o in outcomes if o["band"] and o["status"].startswith(("generated", "rendered")))
    by_code = Counter(c for o in outcomes for c in o["codes"]
                      if o["status"].startswith(("generated", "rendered")))
    def cell(v):
        return "-" if v is None else v

    order = ([l["Id"] for _, l in picks] if picks else sorted(o["id"] for o in outcomes))
    print(f"\n{'Id':<6} {'domain':<26} {'old CRM':>7} {'report':>6} {'to write':>8} "
          f"{'codes':<15} {'status':<20} {'fit':<22} PDF / URL")
    mismatched = []
    for o in sorted(outcomes, key=lambda o: order.index(o["id"]) if o["id"] in order else 0):
        written = o["status"].startswith(("generated", "rendered", "no_gap"))
        to_write = (o["crm_values"] or {}).get("GapScore") if written else None
        if o["score"] is not None and o["score"] != to_write:
            mismatched.append(o["id"])
        print(f"{o['id']:<6} {(o['domain'] or '-')[:26]:<26} {cell(o['crm_score']):>7} "
              f"{cell(o['score']):>6} {cell(to_write):>8} {' '.join(o['codes']) or '-':<15} "
              f"{o['status']:<20} {o['fit'] or '-':<22} {o['url'] or o['error'] or ''}")
    scored = [o for o in outcomes if o["score"] is not None]
    print(f"\nreport score == GapScore to write: "
          + ("yes, for all " + str(len(scored)) + " scored leads" if not mismatched
             else f"NO -- mismatch on {mismatched}"))
    print(f"\ntotal {len(outcomes)}: " + ", ".join(f"{k} {v}" for k, v in by_status.most_common()))
    if by_band:
        print("by band: " + ", ".join(f"{k} {v}" for k, v in by_band.most_common()))
    if by_code:
        print("by finding code: " + ", ".join(f"{k} {v}" for k, v in sorted(by_code.items())))

    path = DATA / f"report_run_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Id", "company", "domain", "status", "score", "codes", "DownloadReportUrl"])
        for o in sorted(outcomes, key=lambda o: o["id"]):
            w.writerow([o["id"], o["company"], o["domain"], o["status"], o["score"],
                        " ".join(o["codes"]), o["url"] or ""])
    print(f"run CSV: {path.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--id", type=int, help="one lead by its CRM Id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sample-bands", action="store_true")
    ap.add_argument("--ids-from-last-sample", action="store_true")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only the leads whose CRM ReportStatus is 'failed'")
    ap.add_argument("--skip-last-sample", action="store_true",
                    help="leave out the leads in data/last_sample_ids.json")
    ap.add_argument("--preflight", action="store_true", help="run the checks and exit")
    ap.add_argument("--scan-one", help=argparse.SUPPRESS)
    ap.add_argument("--crm-write", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.scan_one:
        asyncio.run(_scan_worker(json.loads(args.scan_one)))
        return
    if args.crm_write:
        _crm_write_direct(json.loads(args.crm_write))
        return

    conn = pia.bounded(crm_push.connect)()
    print(f"target {config.CRM_DB_NAME}.{TABLE}, Source={pia.SOURCE}, ScrapeStatus=done")
    preflight(conn, r2=args.preflight or not args.dry_run)
    if args.preflight:
        return
    if PAUSE_FILE.exists():
        sys.exit(f"paused ({PAUSE_FILE.name} present) -- delete it to run")

    leads = pia.bounded(fetch_leads)(conn)
    conn.close()
    by_id = {l["Id"]: l for l in leads}
    picks = None
    if args.id:
        if args.id not in by_id:
            sys.exit(f"Id {args.id} is not a PartnerImport lead marked done")
        todo = [by_id[args.id]]
    elif args.ids_from_last_sample:
        ids = [x["id"] for x in json.loads(SAMPLE_FILE.read_text())["leads"]]
        todo = [by_id[i] for i in ids if i in by_id]
        picks = [(x["bucket"], by_id[x["id"]]) for x in json.loads(SAMPLE_FILE.read_text())["leads"]
                 if x["id"] in by_id]
    elif args.sample_bands:
        picks = pick_sample(leads)
        todo = [l for _, l in picks]
    elif args.retry_failed:
        todo = sorted((l for l in leads if l["ReportStatus"] == "failed"), key=run_order)
    else:
        todo = sorted(leads, key=run_order)
    if args.skip_last_sample:
        sample = {x["id"] for x in json.loads(SAMPLE_FILE.read_text())["leads"]}
        before = len(todo)
        todo = [l for l in todo if l["Id"] not in sample]
        print(f"skipping the {before - len(todo)} sample leads in {SAMPLE_FILE.name}")

    repush_only = {}
    if not args.dry_run:
        current = db.current_reports(TEMPLATE_VERSION, rf.CATALOGUE_VERSION)
        explicit = args.id or args.ids_from_last_sample
        kept = []
        for l in todo:
            rep = current.get(l["Id"])
            if rep and rep["crm_pushed_at"] and not explicit:
                continue                        # done at these versions
            if rep and not rep["crm_pushed_at"]:
                repush_only[l["Id"]] = rep      # rendered, CRM write never landed
            kept.append(l)
        if len(kept) != len(todo):
            print(f"{len(todo) - len(kept)} leads already have a current report -- skipped")
        todo = kept
    if args.limit:
        todo = todo[: args.limit]

    if not args.dry_run and todo:
        print(f"CRM backup of the six scan columns: {backup_crm(todo).relative_to(ROOT)}")
    print(f"{len(todo)} lead(s) to process{' (dry run)' if args.dry_run else ''}\n")
    outcomes, paused = asyncio.run(run(todo, args.dry_run, repush_only))
    if paused:
        print(f"\nstopped: {PAUSE_FILE.name} appeared, {paused} leads not started")
    summarise(outcomes, picks)
    if args.dry_run:
        print("dry run -- no upload, no reports rows, no CRM writes "
              "(\"to write\" is what the CRM would get)")


if __name__ == "__main__":
    main()
