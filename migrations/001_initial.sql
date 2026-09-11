-- 001_initial.sql — baseline schema for the Consiva lead pipeline
--
-- Generated from the working database; apply with `python migrate.py`.
--
-- The `companies` table IS the pipeline's state. Every stage reads rows at one
-- status and writes them to the next, which is what makes the whole thing
-- idempotent, resumable, and safe across a power cut: there is no job queue and
-- no in-memory progress, so restarting anything continues from the status
-- column.

CREATE TABLE companies (
    cin text NOT NULL,
    company_name text NOT NULL,
    roc_code text,
    category text,
    subcategory text,
    company_class text,
    authorized_capital double precision,
    paidup_capital double precision,
    registration_date text,
    registered_address text,
    listing_status text,
    company_status text,
    state_code text,
    indian_foreign text,
    nic_code integer,
    industrial_classification text,
    status text DEFAULT 'discovered'::text NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_domain text,
    tld_used text,
    resolve_confidence double precision,
    has_cmp boolean,
    cmp_name text,
    pre_consent_cookies jsonb,
    founder_name_site text,
    company_tagline text,
    notable_open_roles jsonb,
    open_roles_count integer,
    notable_clients jsonb,
    recent_blog_title text,
    recent_blog_date text,
    tech_cms text,
    tech_ecommerce_platform text,
    tech_payment_gateway text,
    tech_analytics_tools jsonb,
    tech_marketing_tools jsonb,
    team_headcount_site integer,
    footer_address text,
    footer_phone text,
    linkedin_employee_count integer,
    linkedin_industry text,
    linkedin_hq text,
    linkedin_size_band text,
    linkedin_website text,
    business_scale text,
    scale_confidence text,
    scale_basis text,
    apollo_person_id text,
    contact_name text,
    contact_title text,
    contact_email text,
    contact_source text,
    title_match_confidence text,
    consent_gap_summary text,
    has_cookie_banner boolean,
    has_privacy_policy boolean,
    consent_gap_score integer,
    final_domain text,
    cookies_total integer,
    tracking_cookie_count integer,
    third_party_domains jsonb,
    third_party_count integer,
    max_cookie_lifetime_days integer,
    resolve_verified_by text,
    reg_pin text,
    reg_state text,
    reg_locations jsonb,
    resolve_match_score integer,
    resolve_evidence text,
    has_data_form boolean,
    form_field_types jsonb,
    form_consent_checkbox boolean,
    form_third_party_hosts jsonb,
    form_embed_provider text,
    linkedin_url text,
    delivered_at timestamp with time zone
);
CREATE TABLE pipeline_runs (
    id bigint NOT NULL,
    run_date date DEFAULT CURRENT_DATE NOT NULL,
    stage text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    exit_code integer,
    records_in integer,
    leads_after integer,
    note text
);
CREATE SEQUENCE pipeline_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE pipeline_runs_id_seq OWNED BY pipeline_runs.id;
ALTER TABLE ONLY pipeline_runs ALTER COLUMN id SET DEFAULT nextval('pipeline_runs_id_seq'::regclass);
ALTER TABLE ONLY companies
    ADD CONSTRAINT companies_pkey PRIMARY KEY (cin);
ALTER TABLE ONLY pipeline_runs
    ADD CONSTRAINT pipeline_runs_pkey PRIMARY KEY (id);
CREATE INDEX idx_companies_delivered ON companies USING btree (delivered_at) WHERE (delivered_at IS NULL);
CREATE INDEX idx_companies_gap_score ON companies USING btree (consent_gap_score DESC);
CREATE INDEX idx_companies_has_cmp ON companies USING btree (has_cmp);
CREATE INDEX idx_companies_resolved_domain ON companies USING btree (resolved_domain);
CREATE INDEX idx_companies_status ON companies USING btree (status);
CREATE INDEX idx_runs_date ON pipeline_runs USING btree (run_date DESC, started_at DESC);
