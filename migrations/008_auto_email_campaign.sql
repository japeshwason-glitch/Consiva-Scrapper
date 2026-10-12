-- 008_auto_email_campaign.sql -- Auto Email Campaign, milestone 1 (campaign/)
--
-- Eight ae_ tables for the pipeline that DRAFTS one first email per PartnerImport
-- company. It never sends: a later milestone writes the draft into AE_ columns on
-- the CRM's CRM_Scrapper and the CRM team sends it. company_id is the Id of the
-- CRM_Scrapper row (the same value the report tables call lead_ref); the CRM is a
-- different database, so it is a plain integer, not a foreign key.
--
-- Nothing here alters or reads the existing pipeline tables.

CREATE TABLE ae_companies (
    id              BIGSERIAL PRIMARY KEY,
    company_id      INTEGER NOT NULL UNIQUE,
    is_test         BOOLEAN NOT NULL DEFAULT FALSE,
    domain          TEXT,
    report_ref      BIGINT,                -- reports.id of the rendered report, once there is one
    score           INTEGER,
    band            TEXT CHECK (band IN ('A', 'B', 'C', 'D')),
    gap_count       INTEGER,
    report_date     TIMESTAMPTZ,           -- when the scan behind the report ran
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                        'pending', 'checked', 'scanned', 'gate_passed', 'reported',
                        'contact_ranked', 'verified', 'drafted', 'written_to_crm', 'sent',
                        'expired', 'skipped_invalid', 'no_website', 'scan_failed',
                        'score_too_high', 'no_gaps', 'held_no_contact',
                        'held_already_contacted', 'suppressed', 'unscannable', 'legacy')),
    status_reason   TEXT,
    scan_attempts   INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_ae_companies_status ON ae_companies (status, id);
CREATE INDEX idx_ae_companies_test ON ae_companies (is_test);

CREATE TABLE ae_contacts (
    id              BIGSERIAL PRIMARY KEY,
    contact_id      TEXT NOT NULL UNIQUE,  -- crm.CrmCompanyContacts.Id (a uniqueidentifier, kept as text)
    company_id      INTEGER NOT NULL REFERENCES ae_companies (company_id),
    email           TEXT,
    job_title       TEXT,
    rank            INTEGER,
    rank_reason     TEXT,
    nb_verdict      TEXT,
    nb_decision     TEXT NOT NULL DEFAULT 'NOT_CHECKED'
                    CHECK (nb_decision IN ('OK', 'RISKY', 'DO_NOT_EMAIL', 'NOT_CHECKED')),
    nb_checked_at   TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_ae_contacts_company ON ae_contacts (company_id);
CREATE INDEX idx_ae_contacts_email ON ae_contacts (LOWER(email));

CREATE TABLE ae_drafts (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          INTEGER NOT NULL UNIQUE REFERENCES ae_companies (company_id),
    contact_id          TEXT,
    to_email            TEXT,
    subject             TEXT,
    body_html           TEXT,
    band                TEXT CHECK (band IN ('A', 'B', 'C', 'D')),
    draft_status        TEXT NOT NULL DEFAULT 'ready'
                        CHECK (draft_status IN ('ready', 'risky', 'expired', 'sent')),
    template_version    TEXT,
    prompt_version      TEXT,
    generated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    written_to_crm_at   TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ
);

CREATE TABLE ae_suppression (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE CHECK (email = LOWER(email)),
    reason      TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE ae_verification_cache (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE CHECK (email = LOWER(email)),
    verdict     TEXT NOT NULL,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE ae_title_cache (
    id                BIGSERIAL PRIMARY KEY,
    normalized_title  TEXT NOT NULL UNIQUE,
    function          TEXT,
    seniority         TEXT,
    relevance         TEXT,
    source            TEXT NOT NULL CHECK (source IN ('rule', 'llm')),
    prompt_version    TEXT
);

CREATE TABLE ae_flags (
    flag        TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO ae_flags (flag, value) VALUES ('kill_switch', 'off');

CREATE TABLE ae_watermark (
    job_name     TEXT PRIMARY KEY,
    last_crm_id  INTEGER NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
