# One-page DPDP Consent Readiness Reports

A one-time job that gives every **PartnerImport lead marked done** in the
sales team's `dbo.CRM_Scrapper` a one-page A4 PDF: what its homepage does
before a visitor consents, mapped to sections of the DPDP Act, 2023. The PDF
is uploaded to Cloudflare R2 and its public URL is written to the lead's
`DownloadReportUrl`.

No model is involved anywhere. Findings, wording, summary and score are
deterministic functions of one homepage scan; the same scan always produces
the same report.

## Flow (per lead)

```
dbo.CRM_Scrapper (Source='PartnerImport', ScrapeStatus='done')
  -> scan the homepage            scrape_one(report_mode=True), own process, killed at 150 s, 3 attempts;
                                  https, http, then www. (homepage_urls) if the bare domain fails
  -> save the scan                report_scans (Postgres)
  -> findings                     report_findings.evaluate()  -> report_findings (Postgres)
  -> unscannable / failed?        CRM: ReportStatus + ReportGeneratedAt only. Stop.
  -> no gap?                      CRM: six findings + UpdatedAt, DownloadReportUrl = NULL,
                                  ReportStatus = 'no_gap'. Stop.
  -> render                       Jinja2 (autoescape) -> Chromium page.pdf(A4), fitted to one page
  -> upload                       R2: r/<32 hex>.pdf -> reports (Postgres)
  -> CRM write                    six findings + UpdatedAt, DownloadReportUrl, ReportStatus='generated',
                                  ReportGeneratedAt -> reports.crm_pushed_at
```

`ReportStatus` values: `generated`, `no_gap`, `unscannable`, `failed`.

**Unscannable** means the homepage answered HTTP >= 400, was a bot check /
challenge page (`fingerprints.is_blocked`), or was parked, a placeholder or
unfinished (`fingerprints.is_parked`). **Failed** means no scan completed, or a
report could not be rendered to exactly one page.

## What the job writes to SQL Server

Only these ten columns, only on `Source = 'PartnerImport' AND ScrapeStatus =
'done'` rows, only by `UPDATE` (`report_build.crm_update_sql`, the single SQL
Server write in the job; tests enforce the list):

| Column | When |
|---|---|
| `GapScore`, `TrackersPreConsent`, `ThirdPartiesCount`, `CookieBanner`, `CollectsViaForm`, `ConsentFinding` | `generated` and `no_gap` only -- built by `partner_import_analyse.values_for()`, exactly as the partner job and the partner rescan build them |
| `UpdatedAt` | together with those six, UTC now |
| `DownloadReportUrl` | the R2 URL on `generated`; NULL on `no_gap` |
| `ReportStatus`, `ReportGeneratedAt` | every lead; UTC now |

An unscannable or failed scan never overwrites the six findings: it measured a
wall or nothing, not the site. The table is never altered.

Before a run's first write, the six finding columns of every row the run may
touch are saved to `data/crm_score_backup_<timestamp>.json`.

## One source of truth for the score

The PDF score **is** the CRM `GapScore`: `report_findings.score()` calls
`fingerprints.consent_score()`, the function every scan path uses, over the
same scan. A lead whose two numbers ever differ is marked failed rather than
printed. 0 = most severe, 100 = no measured gap. On the PDF: score <= 50 ->
"Needs priority attention", otherwise "Needs attention".

CMP rule (shared formula, since 2026-10-06), by `cmp_evidence`:

| CMP seen as | Tracking cookies | Score |
|---|---|---|
| `script_src` (vendor's script host) or `dom` (its banner element / JS global is live) | none | 100 |
| `script_src` / `dom` | firing | scored like any site |
| `text` (name only in the page source) | any | treated as no CMP |

Rows stored before this rule are not re-scored; each picks it up on its next scan.

## Findings

Defined in `report_catalogue.yaml` (catalogue version `"2"`); fired by
`report_findings.evaluate()`. Tracking cookies are advertising /
session_replay / analytics only -- `necessary` and `unclassified` cookies never
count.

| Code | Kind | Fires when |
|---|---|---|
| G5 | gap | real CMP (`script_src`/`dom`) and >= 1 tracking cookie |
| G1 | gap | no real CMP and >= 1 tracking cookie (never with G5) |
| G2 | gap | G1 and tracking cookies from third-party domains |
| G3 | gap | G1 and no cookie banner |
| G4 | gap | no privacy policy and (G1 or G5 or a data form) |
| A2 | advisory | a data form with no consent checkbox |

Row order G5, G1, G2, G3, G4; at most 4 gaps and 1 review point. A lead whose
only finding is A2 is `no_gap` (no report).

**YouTube-only leads** (every tracking cookie comes from an embedded YouTube
player -- 89 of 1,053 G1/G5 leads on 2026-10-07) keep their report, with the
catalogue's `youtube_only_note` printed under the gaps table: the embed's
privacy-enhanced (no-cookie) mode avoids those cookies. They run last.

**Every customer-facing word is in `report_catalogue.yaml`**: finding text,
the summary sentences, plurals, the score line, the review heading, "and N
more", and the display names of every CMP and cookie vendor (so internal
labels such as "(WP)" never print). The template keeps only its fixed
headings and the footer disclaimer. Changing any wording means bumping the
catalogue `version`, which makes every report regenerate.

## Fitting one page

`templates/report_one_page.html.j2` is a fixed A4 box. A report that would
overflow is re-rendered, in order: with the review row omitted, then with
less cookie detail (`report_findings.COMPACT_LEVELS`). **A gap is never
dropped.** The overflow is measured in the browser -- the box has
`overflow: hidden`, so a page count alone would miss clipping.

## Storage: bucket and URL mapping

| | |
|---|---|
| Bucket | `R2_REPORTS_BUCKET` (`consiva-reports`) |
| Endpoint | `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`, region `auto` |
| Object key | `r/<secrets.token_hex(16)>.pdf` -- random, not guessable from a company |
| Public URL | `REPORT_PUBLIC_BASE_URL + "/" + key`, e.g. `https://reports-consiva.gignati.com/r/3f9c....pdf` |
| Headers | `Content-Type: application/pdf`, `Content-Disposition: inline; filename="Consiva-DPDP-Readiness-<domain>.pdf"` |

The bucket is connected to the custom domain, so the URL path equals the key.

## Config (`.env.docker`)

`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_REPORTS_BUCKET`, `REPORT_PUBLIC_BASE_URL` (no trailing slash),
`REPORT_SALES_CONTACT` (footer line), plus the existing `CRM_DB_*` and
`DATABASE_URL`.

## Running it

The job runs in its own image and compose profile -- the pipeline image lacks
its libraries, and building it never touches the scheduler or API:

```bash
docker compose --profile reports build reports                       # once, or after requirements-reports.txt changes
R="docker compose --profile reports run --rm --no-deps reports"

$R python migrate.py                                                 # 006 + 007: report tables
$R python -m unittest tests.test_reports tests.test_cookie_classification
$R python report_build.py --preflight                                # CRM columns, fonts, R2 round trip
$R python report_build.py --dry-run --limit 1                        # local PDF only
$R python report_build.py --dry-run --sample-bands                   # one lead per GapScore bucket + one G5
$R python report_build.py --ids-from-last-sample                     # those leads, for real
$R python report_build.py --id 6570                                  # one lead, for real
$R python report_build.py --skip-last-sample                         # the full run, sample leads excluded
```

- `--dry-run`: scans and saves the scan and findings, renders PDFs to
  `data/reports_preview/`, prints the GapScore that **would** be written. No
  upload, no `reports` row, no CRM write.
- Resumable: a lead with a rendered report for the current template and
  catalogue versions is skipped (or only re-pushed if its CRM write never
  landed). Create `data/reports.pause` to stop cleanly.
- Full-run order: G5 first, then G1 with advertising/session-replay vendors,
  then other G1, then the rest, then YouTube-only leads; ties by lowest score.
- `--skip-last-sample` leaves out the leads in `data/last_sample_ids.json`.
- `--retry-failed` processes only the leads whose CRM `ReportStatus` is `failed`.
- DNS: the `reports` service resolves through 1.1.1.1 / 8.8.8.8, not the
  pipeline's unbound. Our own recursion answers NXDOMAIN for sites behind some
  CDNs (cc-ecdn.net, induscdn.com chains -- www.bisleri.com, www.biba.in,
  www.arvind.com), which failed those reports outright. The pipeline still
  uses unbound and is likely affected the same way (open item).
- Every run writes `data/report_run_<timestamp>.csv` (Id, company, domain,
  status, score, codes, DownloadReportUrl).

Each scan runs in its own process and each CRM write in a short-lived child
process, both killed at a hard limit: a frozen page or a pymssql call on a
dropped connection cannot stall the run.

## Files

| File | Role |
|---|---|
| `report_build.py` | the job: pre-flight, lead selection, scan, render, upload, CRM write |
| `report_findings.py` | pure findings engine and score |
| `report_catalogue.yaml` | all report wording and display names |
| `storage.py` | R2 upload |
| `templates/report_one_page.html.j2` | the fixed one-page template |
| `assets/consiva-nav-logo.png`, `assets/fonts/` | logo; IBM Plex Sans/Mono woff2 (SIL OFL, see `SOURCE.txt`), embedded as base64 |
| `stages/03_scrape_playwright.py` | `scrape_one(report_mode=True)`: homepage only, HTTP status, unscannable detection, cookie vendors |
| `fingerprints.py` | `consent_score()`, `cmp_evidence()`, `cookie_vendor()` |
| `migrations/006_reports.sql`, `007_report_scan_crm_values.sql` | `report_scans`, `report_findings`, `reports` |
| `Dockerfile.reports`, `requirements-reports.txt` | the reports image |
| `tests/test_reports.py` | findings, summary, wording lint, one-page fit, score identity, CRM column limits |
