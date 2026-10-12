# Project: Consiva lead-gen pipeline

## Context

B2B lead-gen pipeline for Consiva AI (a DPDP compliance / consent-management
platform), built for Swaran Soft. Single funnel (an earlier social-intent
funnel via Twitter/Reddit was fully scrapped by the CEO -- do not resurrect
it). Flow: MCA registered companies -> domain resolution -> consent-gap
detection -> LinkedIn scale enrichment -> business scale classification ->
contact enrichment -> CRM handoff.

The full architecture and every design decision behind it were worked out
in detail before this repo existed. This file captures the concrete state
and the next steps -- treat it as ground truth over anything you might
otherwise assume about how a lead-gen pipeline "usually" works.

## Current state (already built and tested)

- `schema.sql` -- the `companies` table, currently SQLite syntax. Verified
  with real inserts, status transitions, retry logic, and JSON round-trips.
- `db.py` -- shared DB helper (`get_pending`, `advance_status`, `mark_failed`,
  `mark_not_a_lead`, `count_by_status`). **Currently SQLite. Task 1 below
  migrates this to Postgres.**
- `requirements.txt` -- pinned, tested together in a clean venv. Already
  includes `psycopg2-binary` for the migration.
- `stages/01` through `07` -- placeholder files, docstring only, describing
  each stage's exact status transition. No implementation yet.
- `config.py` -- reads `DATABASE_URL` and API keys from environment
  variables. Never hardcode credentials in a stage script.
- `.env.example` -- template for the real `.env` (not committed).

## Task 1: migrate db.py from SQLite to PostgreSQL

- Replace `sqlite3` with `psycopg2`, using connection pooling
  (`psycopg2.pool.SimpleConnectionPool` or similar) rather than opening a
  fresh connection per call.
- Update `schema.sql` for Postgres: the JSON-as-text columns
  (`pre_consent_cookies`, `notable_open_roles`, `notable_clients`,
  `tech_analytics_tools`, `tech_marketing_tools`) should become native
  `JSONB` columns now that Postgres actually supports that properly.
- Convert `?` placeholders to `%s`.
- **Keep the exact same function signatures.** No stage script should ever
  need to change how it calls `db.py` because of this migration --
  `get_pending(status, limit, extra_where, extra_params)`,
  `advance_status(cin, new_status, **fields)`, `mark_failed(cin, prefix, err)`,
  `mark_not_a_lead(cin, reason)`, `count_by_status()` all stay as-is from
  the caller's perspective.
- Read the connection string from `config.DATABASE_URL`.
- Write a test script that actually runs against a real local Postgres
  instance -- insert dummy rows, advance status, trigger the retry/failure
  path, round-trip a JSONB field -- before considering this done. Don't
  infer correctness from reading the code; run it.

## Task 2 onward: build the stages, in this order, don't skip ahead

1. **`stages/01_ingest_mca.py`** -- reads a filtered MCA CSV (columns: CIN,
   CompanyName, CompanyROCcode, CompanyCategory, CompanySubCategory,
   CompanyClass, AuthorizedCapital, PaidupCapital,
   CompanyRegistrationdate_date, Registered_Office_Address, Listingstatus,
   CompanyStatus, CompanyStateCode, `CompanyIndian/Foreign Company`,
   nic_code, CompanyIndustrialClassification). Bulk-inserts at
   `status='discovered'`. Must be idempotent --running it twice on the same
   file must not duplicate rows (`INSERT ... ON CONFLICT (cin) DO NOTHING`).

2. **`stages/02_resolve_domain.py`** -- name -> slug (strip "Pvt Ltd" /
   "Private Limited" / "LLP", produce a tight and a hyphenated variant) ->
   direct HTTP probe across `.com` / `.in` / `.co.in` / `.net.in` / `.org.in`
   / `.biz`, in that priority order. Confidence-score each resolved page via
   `rapidfuzz` name-match against the page title/body. No search engine
   anywhere in this stage. Reads `db.get_pending("discovered")`, writes via
   `db.advance_status(cin, "resolved", ...)` or `db.mark_failed(cin, "resolve", ...)`.

3. **`stages/03_scrape_playwright.py`** -- one headless page load per
   company doing all of: pre-consent-cookie detection, known-CMP
   fingerprint match, banner/policy presence, team-page founder name,
   company tagline, careers page + open-role count, notable clients,
   recent blog title/date, tech stack (CMS, ecommerce platform, payment
   gateway, analytics/marketing tools), team headcount. **Companies with
   `has_cmp = true` get `db.mark_not_a_lead(cin)` here, not advanced
   further** -- this is the cost-discipline checkpoint that keeps the paid
   stages below from ever running on non-leads. Do not skip or weaken this.

4. **`stages/04_enrich_linkedin_apify.py`** -- pulls **only**
   `db.get_pending("scraped", extra_where="has_cmp = false")`. Never query a
   company that already has a CMP. Resolves company -> LinkedIn company URL,
   calls the Apify LinkedIn Company Scraper actor, writes employee count,
   industry, HQ, size band back.

5. **`stages/05_classify_scale.py`** -- rule-based, no LLM, no external API
   call. Combines `paidup_capital` + `linkedin_employee_count` +
   `team_headcount_site` + `nic_code` into `business_scale` (MSME / SMB /
   Startup / Enterprise) + `scale_confidence` + a human-readable
   `scale_basis` string naming which signals drove the decision.

6. **`stages/06_enrich_contact_apollo.py`** -- Apollo `api_search` (the
   endpoint is `/api/v1/mixed_people/api_search` -- **not**
   `/mixed_people/search`, which 403s on lower-tier plans). Filter by
   `resolved_domain` + title priority (Founder/Co-founder > CEO > CTO > Head
   of Engineering/Product > Marketing Head), then filter to
   `has_email: true`. Cross-check the top match's name against
   `founder_name_site`; if they conflict, note it in
   `title_match_confidence` rather than silently trusting Apollo -- Apollo's
   title data has been confirmed unreliable for smaller companies in real
   testing. Call `people/match` with `reveal_personal_emails: true` on
   exactly one chosen match (1 credit). Lusha as fallback only when Apollo
   has no usable match at all.

7. **`stages/07_export_crm.py`** -- `SELECT` only these 11 fields:
   `company_name, domain, business_scale, contact_name, contact_title,
   contact_email, contact_source, title_match_confidence,
   consent_gap_summary, company_tagline, notable_open_roles` WHERE
   `status='contact_done'`. Write to CSV. Advance each exported row to
   `status='exported'`.

## PartnerImport: the Auto Email Campaign pipeline (`campaign/`)

For PartnerImport companies the campaign pipeline is the only thing that owns **`ScrapeStatus`, `ReportStatus`, the nine
report columns and the `AE_` draft columns** on the sales team's `dbo.CRM_Scrapper`. It takes each company through ten
stages (`python -m campaign.jobs prepare`; run commands with `scripts/run.sh`, see the README):

1 intake -> 2 free email checks -> 3 domain -> 4 homepage scan -> 5 score gate (writes the consent columns) -> 6 one-page
report PDF to R2 (writes the report URL) -> 7 contact ranking -> 8 NeverBounce -> 9 draft -> 10 write the draft into the
CRM `AE_` columns (only with `--write-ae`). Postgres `ae_companies` is the source of truth; the CRM columns mirror it.
Details: `docs/report_pipeline.md`, `docs/crm_schema.md`, migrations 008-010.

**Retired -- do not run, do not revive:**
- `report_build.py` refuses to run as a script (guard at the top; importing it is fine, its scan/render functions are
  reused by `campaign/`). `docs/REPORTS.md` describes it and is kept for the report wording and scoring only.
- `partner_import_analyse.py` (the daily partner job started by `scheduler.py`, `RUN_PARTNER` on by default) also writes
  `ScrapeStatus`; keep it off (`RUN_PARTNER=0`, or never start the `pipeline` profile) while the campaign owns that column.
- `scripts/rescan_partner_consent.py` also writes the CRM; it is not part of the pipeline any more.

**CRM write rules** -- every write to SQL Server goes through `campaign/crm_write.py:update_company()`, nowhere else (a
test enforces it):
- Only these columns, each by name: `ScrapeStatus` and `ReportStatus` (only the values already in the table), the nine
  report columns (`GapScore`, `TrackersPreConsent`, `ThirdPartiesCount`, `CookieBanner`, `CollectsViaForm`,
  `ConsentFinding`, `UpdatedAt`, `DownloadReportUrl`, `ReportGeneratedAt`) and the `AE_Draft*` columns. Every value is
  checked against the format the legacy rows hold; `UpdatedAt`, `ReportGeneratedAt` and `AE_DraftGeneratedAt` are stamped by
  the server (`SYSUTCDATETIME()`), never by Python. Never ALTER.
- Only `PartnerImport` rows (checked by a read and again in the `WHERE` clause). Never a **test** company, never a
  **legacy** company (one the retired script already reported), never one not in `ae_companies`.
- **Only when the connected Postgres database is the configured primary (`AE_PRIMARY_DATABASE`, default `consiva`).** A
  throwaway, test or demo database can never drive a real CRM write.
- A write happens in the same step as the matching `ae_companies` status change (`status.set_status`); if the CRM write
  fails the status change is undone and the stage retries.
- `AE_DraftStatus` `ready`/`risky` requires a template version that is not a placeholder; a placeholder draft is written
  as `expired` and only when `AE_ALLOW_PLACEHOLDER_AE_WRITE` is on (demo). `AE_DraftContactId` (an int in the CRM) and
  `AE_DraftSentAt` are never written.
- After an interrupted or "rejected" command, check whether it actually ran and what it changed before going on.

**Test cohort rules** (`prepare --sample N`): N legacy companies are re-opened as `is_test` and run every stage (scans, report
PDFs and R2 uploads, the model calls) but **never write to the CRM, in any column**. Mock NeverBounce verdicts are allowed
for the test cohort only; a production company can only reach `verified` and be drafted on a **live** verdict (stages 8 and
9 each enforce it). Production companies are taken in by the scheduled job but not processed past intake until
`AE_PRODUCTION_ENABLED=1` or `--production`.

**Reports** (the PDF) are still deterministic: no model, no screenshots, exactly one page, never dropping a gap to fit;
every customer-facing word is in `report_catalogue.yaml`. The PDF score is the CRM `GapScore`
(`fingerprints.consent_score()` over the same scan). A homepage that answers HTTP 403 is a retryable `blocked` failure
(after the retries `scan_failed`), not `unscannable`.

## Hard constraints -- do not violate these

- **SQL Server writes come only from `campaign/crm_write.py`, within its allowlist** (see the section above):
  PartnerImport rows only, never test or legacy companies, only from the primary Postgres database, never ALTER.
- **Reports: no model, no screenshots.** Findings, summary and score are deterministic; a report is exactly one page and
  never drops a gap to fit.
- **Email text: a model writes only two sentences, and only under these rules.** Drafts use OpenAI (config; the self-hosted
  server is the fallback provider) for exactly two slots, `opening_line` and `finding_sentence`, fed only by the company's
  scan findings and company-level data. **Never a contact's name, email or phone**; the first name is inserted by code
  afterwards, and a test asserts the request payload holds none of them. Everything else (subject, structure, the report
  link and its anchor text, the call to action, the closing) is fixed in `templates/email/`, whose wording is a
  PLACEHOLDER until the CEO and sales team approve it. Every slot is validated in code (numbers and names must come from
  the findings, banned words, length, no URLs, emails or legal claims); a bad slot is regenerated once and then replaced by a
  plain fallback written in code. The title classifier (contact ranking) sends only the job-title text. The CRM person
  sends and owns all outreach; the pipeline only drafts. Do not add a model call anywhere else without asking.
- **No social scraping.** Twitter/X and Reddit were both fully removed from
  scope by the CEO. Don't reintroduce them.
- **No SearXNG, crt.sh, or Common Crawl** anywhere in discovery. MCA data +
  direct HTTP probing only -- this was a deliberate decision after the
  alternatives proved unreliable or spam-dominated.
- **Every stage must be idempotent and resumable.** A crash mid-batch,
  followed by a rerun, must pick up exactly where it left off via the
  `status` column -- never reprocess or duplicate a row.
- **All DB access goes through `db.py`.** No raw SQL inside a stage script.
- **Test every stage against a real small batch (10-50 companies) before
  calling it done.** Do not infer correctness from code review alone.

## Reference

- `schema.sql` -- full column list and the complete status-flow values.
- `requirements.txt` -- exact dependency versions, already tested together.
