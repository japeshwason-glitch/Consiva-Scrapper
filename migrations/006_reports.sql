-- 006_reports.sql -- one-page DPDP reports for PartnerImport leads
--
-- Three tables, all keyed back to the lead by lead_ref (the Id of the row in
-- the CRM's dbo.CRM_Scrapper, which has no CIN). See docs/REPORTS.md.
--
--   report_scans     one homepage scan in report mode, every attempt kept
--   report_findings  the catalogue codes a scan produced
--   reports          one rendered PDF per (scan, template, catalogue) version
--
-- Nothing in the lead pipeline reads or writes these.

CREATE TABLE report_scans (
    id                            BIGSERIAL PRIMARY KEY,
    lead_ref                      INTEGER NOT NULL,
    company_name                  TEXT,
    requested_domain              TEXT,
    final_url                     TEXT,
    final_domain                  TEXT,
    http_status                   INTEGER,
    scan_status                   TEXT NOT NULL
                                  CHECK (scan_status IN ('ok', 'unscannable', 'failed')),
    unscannable_reason            TEXT,
    scanned_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scanner_sha                   TEXT,
    fingerprints_version          TEXT,
    has_cmp                       BOOLEAN,
    cmp_name                      TEXT,
    cmp_evidence                  TEXT
                                  CHECK (cmp_evidence IN ('script_src', 'dom', 'text')),
    has_cookie_banner             BOOLEAN,
    has_privacy_policy            BOOLEAN,
    cookies                       JSONB,      -- every cookie, with category and vendor; never values
    tracking_cookie_count         INTEGER,
    third_party_tracking_domains  JSONB,
    has_data_form                 BOOLEAN,
    form_field_types              JSONB,
    form_consent_checkbox         BOOLEAN,
    form_privacy_link             BOOLEAN,
    readiness_score               INTEGER,
    attempts                      INTEGER,
    error                         TEXT
);
CREATE INDEX idx_report_scans_lead ON report_scans (lead_ref, scanned_at DESC);

CREATE TABLE report_findings (
    id                 BIGSERIAL PRIMARY KEY,
    scan_id            BIGINT NOT NULL REFERENCES report_scans(id),
    code               TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('gap', 'advisory')),
    dpdp_sections      TEXT,
    evidence           JSONB,
    catalogue_version  TEXT NOT NULL
);
CREATE INDEX idx_report_findings_scan ON report_findings (scan_id);

CREATE TABLE reports (
    id                 BIGSERIAL PRIMARY KEY,
    scan_id            BIGINT NOT NULL REFERENCES report_scans(id),
    lead_ref           INTEGER NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('rendered', 'failed', 'superseded')),
    template_version   TEXT NOT NULL,
    catalogue_version  TEXT NOT NULL,
    object_key         TEXT,
    public_url         TEXT,
    pdf_sha256         TEXT,
    size_bytes         INTEGER,
    generated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    crm_pushed_at      TIMESTAMPTZ,
    error              TEXT,
    UNIQUE (scan_id, template_version, catalogue_version)
);
CREATE INDEX idx_reports_lead ON reports (lead_ref, template_version, catalogue_version, status);
