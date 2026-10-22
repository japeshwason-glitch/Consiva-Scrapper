-- 012_batches.sql -- Auto Email Campaign: held-out batches
--
-- A company taken in by a named batch run (`prepare --batch NAME`, e.g. gf_clutch for the GoodFirms and Clutch
-- companies) carries that name here. Every stage, the funnel and the scheduled jobs (daily, housekeeping) only see
-- rows whose batch IS NULL, so the scheduler never touches a batch company; a batch run only sees its own batch.
-- Existing rows stay NULL, which is "normal pipeline". Nothing here touches the CRM.
ALTER TABLE ae_companies ADD COLUMN batch TEXT;
CREATE INDEX idx_ae_companies_batch ON ae_companies (batch, status, id);
