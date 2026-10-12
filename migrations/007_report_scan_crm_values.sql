-- 007_report_scan_crm_values.sql -- the report score IS the CRM GapScore
--
-- report_findings.score() is fingerprints.consent_score(), and that formula
-- reads whether a form posts to a third-party host -- the one input the scan
-- row did not yet keep.
--
-- crm_values records the six CRM columns written back from this scan
-- (GapScore, TrackersPreConsent, ThirdPartiesCount, CookieBanner,
-- CollectsViaForm, ConsentFinding), so every CRM value can be traced to the
-- scan that produced it. Our table only; the CRM schema is never altered.
ALTER TABLE report_scans ADD COLUMN form_third_party_hosts JSONB;
ALTER TABLE report_scans ADD COLUMN crm_values JSONB;
