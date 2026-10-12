-- 009_campaign_stages.sql -- Auto Email Campaign, milestone 2 (stages 2-8)
--
-- Columns the stages need on the milestone-1 tables, plus three small tables: an MX
-- cache, a NeverBounce credit ledger, and nothing else. Existing pipeline tables are not touched.

-- A fourth way for a company to stop: another company already holds its domain.
ALTER TABLE ae_companies DROP CONSTRAINT ae_companies_status_check;
ALTER TABLE ae_companies ADD CONSTRAINT ae_companies_status_check CHECK (status IN (
    'pending', 'checked', 'scanned', 'gate_passed', 'reported',
    'contact_ranked', 'verified', 'drafted', 'written_to_crm', 'sent',
    'expired', 'skipped_invalid', 'no_website', 'scan_failed',
    'score_too_high', 'no_gaps', 'held_no_contact',
    'held_already_contacted', 'suppressed', 'unscannable', 'legacy',
    'held_duplicate_domain'));

ALTER TABLE ae_companies
    ADD COLUMN company_name      TEXT,          -- the CRM CompanyName, for the scanner and the report
    ADD COLUMN website           TEXT,          -- the CRM Website value as read, before any cleaning
    ADD COLUMN company_scale     TEXT,          -- the CRM CompanyScale as read (MSME, 1-50, Enterprise, ...)
    ADD COLUMN domain_source     TEXT CHECK (domain_source IN ('website', 'email')),
    ADD COLUMN scan_id           BIGINT,        -- report_scans.id the gate decision rests on
    ADD COLUMN scan_seconds      NUMERIC(8, 2), -- wall time of the last scan attempt
    ADD COLUMN failure_category  TEXT CHECK (failure_category IN ('dns', 'timeout', 'blocked', 'http_error', 'other')),
    ADD COLUMN report_url        TEXT,
    ADD COLUMN chosen_contact_id TEXT,          -- ae_contacts.contact_id picked by stage 8
    ADD COLUMN risky             BOOLEAN NOT NULL DEFAULT FALSE;  -- verified, but only to a RISKY address

ALTER TABLE ae_contacts
    ADD COLUMN check_passed      BOOLEAN,       -- stage 2: NULL = not checked yet
    ADD COLUMN check_reason      TEXT,          -- why it failed, e.g. role_address, no_mx
    ADD COLUMN checked_at        TIMESTAMPTZ,
    ADD COLUMN title_function    TEXT,          -- stage 7 classification of job_title
    ADD COLUMN title_seniority   INTEGER,
    ADD COLUMN title_relevance   INTEGER,
    ADD COLUMN title_source      TEXT CHECK (title_source IN ('rule', 'llm', 'unclassified', 'none'));

-- MX lookups, cached per domain so a domain shared by many contacts is asked about once.
CREATE TABLE ae_mx_cache (
    domain      TEXT PRIMARY KEY,
    has_mx      BOOLEAN,                        -- NULL = the lookup itself failed (timeout): not cached as an answer
    detail      TEXT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- NeverBounce credits spent per calendar day (IST), for the daily cap.
CREATE TABLE ae_neverbounce_usage (
    day           DATE PRIMARY KEY,
    credits_used  INTEGER NOT NULL DEFAULT 0
);
