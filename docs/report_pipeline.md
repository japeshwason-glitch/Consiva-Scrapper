# Report pipeline: what exists today, and what the Auto Email Campaign needs

Read-only mapping, written 2026-10-12. Nothing in the repo was changed to
produce it. Companion to `docs/REPORTS.md` (which describes the job's rules);
this file is about how to call it from another job. The CRM side is in
`docs/crm_schema.md`.

## A. The pipeline as it is

### Entry points

There is exactly one: the CLI `python report_build.py [flags]`, run in the
`reports` compose profile. No queue, no function meant to be imported, no API
route. Flags: `--preflight`, `--dry-run`, `--limit N`, `--id <CRM Id>`,
`--sample-bands`, `--ids-from-last-sample`, `--skip-last-sample`,
`--retry-failed`; `--scan-one` and `--crm-write` are internal worker modes the
job re-invokes itself with.

Input is **not** a domain. `main()` selects its own leads, hard-wired:
`SELECT Id, CompanyName, Website, <six CRM columns>, DownloadReportUrl,
ReportStatus FROM dbo.CRM_Scrapper WHERE Source='PartnerImport' AND
ScrapeStatus='done'` (`fetch_leads`, `report_build.py:188`). The domain is
derived per lead with `partner_import_analyse.domain_of(Website)`.

### Flow, step by step

| Step | Where | Notes |
|---|---|---|
| Pick leads, order them | `main()`, `run_order()` | G5 first, then ad/session-replay G1, other G1, rest, YouTube-only last |
| Skip finished leads | `db.current_reports()` in `main()` | version-based, see Idempotency |
| CRM backup | `backup_crm()` | six finding columns to `data/crm_score_backup_*.json`, before any write |
| Scan | `scan()` -> subprocess `--scan-one` -> `_scan_worker()` -> `stages/03_scrape_playwright.scrape_one(browser, company, False, report_mode=True)` | one Chromium per attempt, homepage only, no LLM |
| Save scan | `scan_row()` -> `db.insert_report_scan()` | table `report_scans` |
| CRM values | `crm_values()` -> `partner_import_analyse.values_for()` | the six CRM columns, byte-identical to the partner job |
| Findings and score | `report_findings.evaluate()` | pure; `score()` = `fingerprints.consent_score()`; wording from `report_catalogue.yaml` |
| Save findings | `db.insert_report_findings()` | table `report_findings` |
| Score identity check | `process()` ~line 582 | report score must equal the CRM GapScore, else the scan is marked failed |
| Render | `render_fitted()` -> `render_pdf()` | Jinja2 template -> Chromium `page.pdf(A4)`, measured to be exactly one page |
| Upload | `storage.upload_report()` | R2 key `r/<32 hex>.pdf`, public URL `REPORT_PUBLIC_BASE_URL/<key>` |
| Save report | `db.insert_report()` | table `reports`; supersedes older rendered rows for the lead |
| CRM write | `crm_write()` -> `crm_update_sql()` in a child process | the ten allowed columns, UPDATE only |
| Mark pushed | `db.mark_report_pushed()` | `reports.crm_pushed_at` |

### Outputs: where each value ends up

Postgres (`migrations/006`, `007`):

| Value | Location |
|---|---|
| Score | `report_scans.readiness_score` (set only when the scan was `ok`); also `report_scans.crm_values->>'GapScore'` |
| Gap count | **not stored.** Derive it: `COUNT(*) FROM report_findings WHERE scan_id = ? AND kind = 'gap'`. The PDF prints it ("N gaps") but no column holds it |
| Top findings | `report_findings` rows (`code`, `kind`, `dpdp_sections`, `evidence` JSONB, `catalogue_version`). At most 4 gaps and 1 advisory per scan |
| Scan date | `report_scans.scanned_at` (TIMESTAMPTZ); `reports.generated_at` for the PDF |
| Report URL | `reports.public_url`, with `object_key`, `pdf_sha256`, `size_bytes` |
| Raw evidence | `report_scans`: `cookies`, `third_party_tracking_domains`, `form_*`, `cmp_*`, `has_cookie_banner`, `has_privacy_policy` |
| Scan failures | `report_scans.scan_status` (`ok`/`unscannable`/`failed`), `unscannable_reason`, `error`, `attempts` |

SQL Server (`dbo.CRM_Scrapper`, ten columns only): `GapScore`,
`TrackersPreConsent`, `ThirdPartiesCount`, `CookieBanner`, `CollectsViaForm`,
`ConsentFinding`, `UpdatedAt`, `DownloadReportUrl`, `ReportStatus`
(`generated`/`no_gap`/`unscannable`/`failed`), `ReportGeneratedAt`.

Everything is keyed by `lead_ref` = the `Id` of a `dbo.CRM_Scrapper` row. There
is no domain key in `reports`; `report_scans` has `requested_domain` and
`final_domain` but no index on either.

### Can another job call it for one domain?

Not cleanly today. What is reusable and what is not:

- **Reusable as-is:** `report_findings.evaluate(scan_row)` (pure),
  `render_fitted()` / `render_html()` (take a domain, date and scan dict),
  `storage.upload_report()`, `fingerprints.consent_score()`, and the
  `db.insert_report_*` helpers.
- **Reusable with a thin wrapper:** `scan(lead, domain)` only reads
  `lead["Id"]` and `lead["CompanyName"]` and sets `cin = "partner:<Id>"`, so a
  caller can pass any dict with those two keys. `process()` is the whole
  per-lead pipeline but is welded to the CRM: its `finish()` always calls
  `crm_write()`, whose UPDATE is constrained to `Source='PartnerImport' AND
  ScrapeStatus='done'` on `dbo.CRM_Scrapper`. `--dry-run` skips the CRM write
  and the upload, so there is no "do everything except the CRM write" mode.
- **Not importable cheaply:** `report_build` imports `partner_import_analyse`,
  which does `import_module("03_scrape_playwright")` and pulls Playwright,
  `crm_push` (pymssql) and `personas`. Importing the module also runs
  `_sha()` over the template and stage 03 at import time, and needs the
  `reports` image (Jinja2, pypdf, boto3).
- `main()` is CLI-only; lead selection, ordering, sampling and the backup are
  inline in it, not functions.

For the prepare job, the call needed is roughly
`make_report(domain, lead_ref) -> {scan_id, report_id, score, gap_count, codes,
public_url, status}`, built from `scan()` + `rf.evaluate()` + `render_fitted()`
+ `storage.upload_report()` + `db.insert_*`, with no CRM write. See C.

### Idempotency and freshness

- **Resume key.** `db.current_reports(template_version, catalogue_version)`
  returns the latest `rendered` report per `lead_ref` at the current template
  hash and catalogue version. A lead in that set whose `crm_pushed_at` is set is
  skipped; one that was rendered but never pushed is only re-pushed, not
  rescanned. `--id` and `--ids-from-last-sample` bypass the skip.
- **There is no age-based freshness check.** `reports.generated_at` and
  `report_scans.scanned_at` exist, but nothing compares them to a date. A
  report is "current" until the template hash or catalogue version changes.
  The plan's "reuse a report under 14 days old" must be written new.
- **Same lead scanned twice** (for example `--id`): a second `report_scans` row
  (every attempt is kept), a second `report_findings` set, a new random R2
  object (the old PDF stays in the bucket), the older `reports` row becomes
  `superseded`, and the CRM columns are overwritten. `UNIQUE (scan_id,
  template_version, catalogue_version)` only prevents two reports for one scan.
- **Same domain on two leads:** nothing de-duplicates by domain. Each lead
  gets its own scan, PDF and URL.
- `SCANNER_SHA` (hash of stage 03 + `fingerprints.py`) is stored on every scan
  but is not part of any freshness rule.

### Failure behaviour

| Case | Behaviour | Recorded as |
|---|---|---|
| Scan hangs | whole process group killed at `SCAN_TIMEOUT_S = 150` | retry |
| Scan crashes or returns no data | `SCAN_ATTEMPTS = 3` total, no delay between attempts | `report_scans.scan_status='failed'`, `error`, `attempts`; CRM `ReportStatus='failed'`; no `reports` row |
| Bare domain will not load | tries https, http, then `www.` inside one attempt | |
| HTTP >= 400, bot wall, parked page | not retried | `scan_status='unscannable'` + reason; CRM `ReportStatus='unscannable'` |
| No usable domain in `Website` | scan skipped, attempts 0 | `failed` |
| Render overflow or > 1 page | steps through `FIT_STEPS` (drop review row, then compact levels); gaps never dropped | `reports.status='failed'`, `error`; CRM `failed` |
| Render hangs | `RENDER_TIMEOUT_S = 60` | same |
| Report score != CRM GapScore | scan marked failed rather than printed | `failed` |
| CRM write hangs | child process killed at 45 s, 3 attempts with 10 s and 20 s sleeps | raises; lead outcome `error` |
| Any CRM call hangs in the main process | `pia.bounded()` calls `os._exit(75)` after 60 s | the whole run exits |
| Unhandled exception in one lead | caught in `run().one()`; outcome `error`; nothing written for it | the run CSV only |

Note: an `error` outcome and a hard exit leave no trace in Postgres except
whatever was already inserted.

### Runtime and concurrency

- `CONCURRENCY = 6` leads at once, `render_sem` limits renders to 2. Each scan
  is its own Chromium process; the container has `shm_size: 1gb`.
- Measured, not guessed: the 2026-10-07 full run (`logs/report_run_2026-10-07.log`)
  started 13:02 and its CSV was written 14:56, for 2,351 leads. That is about
  2.9 s of wall time per lead, which is roughly 17 s each at concurrency 6
  (includes the fast unscannable ones). Worst case per lead: 3 x 150 s of scan
  plus 60 s of render.
- Outcomes of that run: generated 1,183, no_gap 862, unscannable 290, failed 16.

### Existing pieces to reuse, not duplicate

- **Config:** `config.py`, all from environment (`.env.docker`). R2, CRM and
  LLM settings already exist (`R2_*`, `REPORT_PUBLIC_BASE_URL`,
  `REPORT_SALES_CONTACT`, `CRM_DB_*`, `CRM_DB_TABLE`, `CRM_LEADS_TABLE`,
  `LLM_BASE_URL`, `LLM_ENABLED`, `LLM_TIMEOUT`). No SendGrid, NeverBounce or
  kill-switch settings exist.
- **Postgres access:** `db.py` (pooled `get_cursor()`, `_insert()`,
  `_serialize_json_fields()`); migrations via `migrate.py` + `migrations/NNN_*.sql`
  (checksummed, transactional, recorded in `schema_migrations`).
- **SQL Server access:** `crm_push.connect(database=None)`, and
  `partner_import_analyse.bounded()` for hang protection. Other scripts write
  raw T-SQL inline (the "no raw SQL in a stage" rule is about the MCA stages and
  `db.py`).
- **Scheduling:** `scheduler.py` (and `run_daily.py`) with a pause-file pattern
  (`data/partner.pause`, `data/reports.pause`).
- **LLM:** `llm.py` talks to the self-hosted llama.cpp server. `chat_json()`
  asks for JSON in the prompt and parses it; it does **not** use llama.cpp's
  `response_format` / `json_schema` / grammar, so "schema-enforced JSON" is new.
- **Logging:** `print()` with timestamps, redirected to `logs/*.log` by shell
  wrappers. No `logging` module anywhere.
- **Tests:** stdlib `unittest`, run in the reports image:
  `docker compose --profile reports run --rm reports python -m unittest tests.test_reports tests.test_cookie_classification`.
  `test_reports.py` already has a test that scans the source for SQL Server
  writes outside the allowed ten columns, so a new job that writes to the CRM
  will trip it by design.

## C. Integration notes

### Where the new jobs should live

Nothing in the repo is a package; scripts sit at the repo root and stages in
`stages/`. Following that: a new `campaign/` directory with `prepare.py`,
`sender.py`, `webhook.py` and shared `campaign/` helpers, plus
`campaign/` migrations numbered `008_...` onward in `migrations/`. The webhook
receiver is always on, so it fits next to `api.py` (a router in the existing
FastAPI app) rather than as a new service, unless you want it isolated from
the pipeline API. The prepare job needs the reports image (Chromium, Jinja2,
boto3); the sender and webhook do not.

### What in the pipeline would need to change (list only)

1. A callable per-domain entry point (scan -> findings -> PDF -> R2 -> rows)
   with no CRM write and no `PartnerImport`/`ScrapeStatus` coupling.
2. Split `report_build` so the scan, render and findings code can be imported
   without `partner_import_analyse` and pymssql, or move them into a module.
3. An age-based freshness check (`scanned_at`/`generated_at` within 14 days),
   keyed by domain, not only by template/catalogue version.
4. Key reports by domain as well as lead: a `domain` column (or index on
   `final_domain`) on `report_scans`/`reports`, and a way to attach one report
   to many leads at the same domain.
5. Store the gap count (and the top finding codes) on `reports` or a view, so
   the score gate does not recount `report_findings`.
6. Score bands A/B/C/D as config, with the gate thresholds read from there.
7. Retry policy at the job level for `scan_failed` (the plan's "retry twice"
   overlaps the scanner's own 3 attempts).
8. Structured logging and a status/reason table for the new lead statuses.
9. Schema-enforced JSON in `llm.py` (grammar or `response_format`).
10. New config: SendGrid, NeverBounce, caps, send window, kill switch.

### Gaps and conflicts between the plan and today

- **Which leads?** The report pipeline only knows `dbo.CRM_Scrapper` rows with
  `Source='PartnerImport'` and `ScrapeStatus='done'`, and `lead_ref` is that
  table's `Id`. The plan says "purchased CRM leads" with a rep and a job title,
  which sounds like `crm.CrmLeads` or another table. Its ids could collide with
  `lead_ref` values.
- **CRM write constraint.** `CLAUDE.md` limits SQL Server writes by the report
  job to ten named columns, never ALTER. Lead status with a reason, suppression
  and send state would need somewhere to live: new Postgres tables keyed by lead
  id are consistent with that rule; new CRM columns are not.
- **Band definitions disagree.** The plan's A/B/C/D are 0-24, 25-49, 50-74,
  75-90; the repo has only two bands (`priority` at score <= 50, else
  `attention`) and different sample buckets in `report_build`.
- **"Skip over 90" vs "zero gaps".** `consent_score()` is 100 when no gap is
  measured; scores 91-99 are possible. `no_gap` today means no gap finding at
  all (an A2-only lead counts as `no_gap`). The two plan gates are not the same
  condition.
- **Lead data lives in three places** (see `docs/crm_schema.md`): the company in
  `dbo.CRM_Scrapper`, emails and titles in `crm.CrmCompanyContacts` (joined on
  `CompanyId = CRM_Scrapper.Id`), the owner in `crm.CrmCompanyOwnership`.
  `CRM_Scrapper` itself has no email or rep column. Contact ranking works over
  contacts (1.58 per company on average, 21.8% free-mail).
- **Reports are not guaranteed to exist for a given domain.** A lead is
  `unscannable` (290 of 2,351 in the Oct 7 run) or `no_gap` (862) without a PDF.
- **G5 and YouTube-only reports.** A report can say a real CMP is present yet
  cookies fire (G5), or list only YouTube embed cookies; both are weak first-email
  hooks. The run order already puts YouTube-only last; the plan has no equivalent.
- **LLM scope.** `llm.py` states it is never used for outreach copy or
  personalisation. Ranking job titles is not copy, but it is not in that
  docstring's allowed list either, and `CLAUDE.md` bans LLM content only for
  outreach text. The email body must stay template-only.
- **"Click/open tracking off" vs reports.** The report URL is the only open
  signal; "report-open logging via a Cloudflare Worker" would mean changing the
  public bucket's serving path, and the URL is `r/<random>.pdf` straight from R2.
- **Existing reports age.** The 2,045 reports from the Oct 7 run become older
  than 14 days on Oct 21 and would be rescanned, even though the score is stored.
- **DNS.** The reports container resolves via 1.1.1.1/8.8.8.8; the pipeline's
  unbound gives false NXDOMAIN for some CDN sites. Any prepare job must run in
  the reports service, not the pipeline image.

### Questions to answer before Milestone 1

1. Which rows are the "purchased CRM leads"? `docs/crm_schema.md` shows the
   candidates are `dbo.CRM_Scrapper` PartnerImport rows (3,539, with 5,487
   contacts in `crm.CrmCompanyContacts`; 2,351 scraped), not `crm.CrmLeads`
   (113 inbound leads). Is `ScrapeStatus='done'` still the right filter?
1a. Every lead is owned by one user, so "assigned rep" selects everything. Will
   real owners be assigned before the campaign, and from which list?
1b. The CRM app already sends email through SendGrid (2,191 sent, events
   ingested by a webhook not in this repo) and 1,415 PartnerImport companies
   have been emailed. Are the new jobs meant to replace that sender, run beside
   it, or exclude those companies? Who owns the existing SendGrid event webhook?
2. Where do prepare/sender statuses live: new Postgres tables (my suggestion),
   or columns in the CRM? Is "never ALTER" still absolute for this work?
3. May the new jobs write `DownloadReportUrl` and the six finding columns, or
   is the existing report job the only writer of those?
4. Do reports for the 2,045 already-scored leads count as fresh on the day the
   campaign starts, or do you want a rescan?
5. Is the 14-day reuse measured from `scanned_at`, and per domain rather than
   per lead?
6. Should G5 (CMP present, cookies still fire) and YouTube-only leads be sent
   at all, and in which band?
7. Is 91-99 "too high" (skip) while 100 is "no gaps"? One rule or two?
8. How many domains per day should the prepare job scan? At about 17 s each at
   concurrency 6, 1,000 domains is roughly 50 minutes.
9. Which SendGrid account, sender domain and unsubscribe group; are the reps'
   addresses already authenticated?
10. Is the llama-server (`LLM_BASE_URL`) reachable from the reports container,
    and does it support grammar or `json_schema` output?
11. Who owns the suppression list, and is there an existing one (CRM
    unsubscribes, partner opt-outs) to import?
12. Should the sender job live in the pipeline compose profile or its own
    profile like `reports`?
