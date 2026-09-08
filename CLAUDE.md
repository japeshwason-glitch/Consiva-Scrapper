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

## Hard constraints -- do not violate these

- **No LLM-generated personalization hooks or written email content,
  anywhere.** The CRM person writes all outreach themselves. Do not add an
  OpenAI/Anthropic API call to any stage for this purpose.
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
