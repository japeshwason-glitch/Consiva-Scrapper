-- Single table, single source of truth for the whole pipeline.
-- Every stage script reads rows at one status and writes them to the next.
--
-- PostgreSQL. See STATUS_FLOW.md for the full status chain.

CREATE TABLE IF NOT EXISTS companies (
    -- Identity (from MCA)
    cin                     TEXT PRIMARY KEY,
    company_name            TEXT NOT NULL,
    roc_code                TEXT,
    category                TEXT,
    subcategory             TEXT,
    company_class           TEXT,
    authorized_capital      DOUBLE PRECISION,
    paidup_capital          DOUBLE PRECISION,
    registration_date       TEXT,
    registered_address      TEXT,
    listing_status          TEXT,
    company_status          TEXT,
    state_code              TEXT,
    indian_foreign          TEXT,
    nic_code                INTEGER,
    industrial_classification TEXT,

    -- Pipeline state machine -- see STATUS_FLOW.md for the full diagram
    status                  TEXT NOT NULL DEFAULT 'discovered',
    retry_count             INTEGER NOT NULL DEFAULT 0,
    last_error              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Stage: domain resolution
    resolved_domain         TEXT,
    tld_used                TEXT,
    resolve_confidence      DOUBLE PRECISION,

    -- Stage: Playwright multi-purpose scrape
    has_cmp                 BOOLEAN,        -- native boolean: query with true/false, never 0/1
    cmp_name                TEXT,
    pre_consent_cookies     JSONB,
    founder_name_site       TEXT,
    company_tagline         TEXT,
    notable_open_roles      JSONB,
    open_roles_count        INTEGER,
    notable_clients         JSONB,
    recent_blog_title       TEXT,
    recent_blog_date        TEXT,
    tech_cms                TEXT,
    tech_ecommerce_platform TEXT,
    tech_payment_gateway    TEXT,
    tech_analytics_tools    JSONB,
    tech_marketing_tools    JSONB,
    team_headcount_site     INTEGER,
    footer_address          TEXT,
    footer_phone            TEXT,

    -- Stage: Apify LinkedIn enrichment
    linkedin_employee_count INTEGER,
    linkedin_industry       TEXT,
    linkedin_hq             TEXT,
    linkedin_size_band      TEXT,
    linkedin_website        TEXT,

    -- Stage: business scale classification
    business_scale          TEXT,           -- MSME / SMB / Startup / Enterprise
    scale_confidence        TEXT,           -- high / moderate / low
    scale_basis             TEXT,           -- which signals fed the decision, human-readable

    -- Stage: Apollo / Lusha contact enrichment
    apollo_person_id        TEXT,
    contact_name            TEXT,
    contact_title           TEXT,
    contact_email           TEXT,
    contact_source          TEXT,           -- apollo / lusha / site_scrape
    title_match_confidence  TEXT            -- high / moderate
);

-- Every stage query filters WHERE status = %s -- this index is load-bearing.
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);

-- Useful for the "skip companies that already have a CMP" cost-discipline check.
CREATE INDEX IF NOT EXISTS idx_companies_has_cmp ON companies(has_cmp);

-- Added after the initial migration. ADD COLUMN IF NOT EXISTS keeps init_db()
-- idempotent against a table that already exists.
-- consent_gap_summary is the factual finding stage 03 hands to the CRM person
-- (which CMP, how many pre-consent cookies, whether a policy page exists).
-- It is assembled deterministically from the scrape -- never written by a model.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS consent_gap_summary TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS has_cookie_banner  BOOLEAN;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS has_privacy_policy BOOLEAN;

-- How much of a consent problem the site actually has (0-100), computed
-- deterministically in stage 03. has_cmp=false only says "no consent platform";
-- this says whether there is a gap worth paying to enrich. Stage 04 filters and
-- orders on it so paid credits go to the strongest leads first.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS consent_gap_score INTEGER;

-- Two CINs can legitimately share one website (group companies). Only one of
-- them should reach the paid stages, or Apify and Apollo get billed twice for
-- the same site and often the same person.
CREATE INDEX IF NOT EXISTS idx_companies_resolved_domain ON companies(resolved_domain);
CREATE INDEX IF NOT EXISTS idx_companies_gap_score ON companies(consent_gap_score DESC);

-- Full pre-consent cookie picture. pre_consent_cookies now holds one object per
-- cookie (name, domain, party, lifetime_days, secure, same_site, category) --
-- never the cookie VALUE, which is a live tracking identifier for a real
-- visitor and has no business in this database.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS final_domain             TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS cookies_total            INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS tracking_cookie_count    INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS third_party_domains      JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS third_party_count        INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS max_cookie_lifetime_days INTEGER;

-- How a domain was accepted: 'fuzzy' (rapidfuzz alone, high confidence) or
-- 'llm' (fuzzy was ambiguous, the local model confirmed the page belongs to
-- this company). Auditable, so a bad batch can be traced to its decision path.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS resolve_verified_by TEXT;

-- Registered-office location, parsed once at ingest. Every MCA address carries
-- a 6-digit PIN (verified: 6973/6973), which anchors the parse. These are the
-- strongest verification signal available for "is this the company's website" --
-- a page quoting the registered PIN or district is near-conclusive, in a way
-- that a matching name never is (marvel.com matches MARVEL KING perfectly).
ALTER TABLE companies ADD COLUMN IF NOT EXISTS reg_pin        TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS reg_state      TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS reg_locations  JSONB;

-- How stage 02 decided this domain belongs to this company: the model's 0-100
-- content-vs-name match score and its one-line reason. Auditable: every
-- accepted domain can be traced to why it was accepted.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS resolve_match_score INTEGER;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS resolve_evidence    TEXT;

-- Data-collection forms. Cookies are only half of DPDP: an enquiry form taking
-- a name, phone or email is personal-data processing and needs notice and
-- consent whether or not a single cookie fires. Without this, ~58% of scraped
-- sites scored zero and were written off.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS has_data_form        BOOLEAN;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS form_field_types     JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS form_consent_checkbox BOOLEAN;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS form_third_party_hosts JSONB;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS form_embed_provider  TEXT;

-- LinkedIn company page URL, kept alongside the enrichment for audit.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS linkedin_url TEXT;

-- Set when the CRM has actually pulled a lead. Without it, an API consumer
-- polling status='exported' re-reads the same rows forever.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_companies_delivered ON companies(delivered_at)
    WHERE delivered_at IS NULL;
