-- Schema the scraper needs on the SALES TEAM'S SQL Server.
--
-- Run once per environment. Applied to CookieConsentDB_Staging already; a
-- production database needs it before the pipeline points at it, or every
-- insert fails on the unknown columns.
--
-- Idempotent: safe to run again, and safe to run on a database that already
-- has some of these.
--
--   sqlcmd -S <host>,<port> -d <database> -U <user> -P <password> \
--          -i deploy/crm_schema.sql

-- ---------------------------------------------------------------------------
-- 1. Outbound leads (the MCA scraper) -> dbo.CRM_Scrapper
-- ---------------------------------------------------------------------------
-- Three job titles worth hunting, best first. Written only for leads that
-- arrived with no contact attached -- about 8 in 10 -- so enrichment knows
-- which titles to ask for instead of guessing.
IF COL_LENGTH('dbo.CRM_Scrapper', 'TargetTitle1') IS NULL
    ALTER TABLE dbo.CRM_Scrapper ADD TargetTitle1 nvarchar(120) NULL;
IF COL_LENGTH('dbo.CRM_Scrapper', 'TargetTitle2') IS NULL
    ALTER TABLE dbo.CRM_Scrapper ADD TargetTitle2 nvarchar(120) NULL;
IF COL_LENGTH('dbo.CRM_Scrapper', 'TargetTitle3') IS NULL
    ALTER TABLE dbo.CRM_Scrapper ADD TargetTitle3 nvarchar(120) NULL;

-- ---------------------------------------------------------------------------
-- 2. Inbound leads (analysed from the lead's own work email) -> crm.CrmLeads
-- ---------------------------------------------------------------------------
IF COL_LENGTH('crm.CrmLeads', 'TargetTitle1') IS NULL
    ALTER TABLE crm.CrmLeads ADD TargetTitle1 nvarchar(120) NULL;
IF COL_LENGTH('crm.CrmLeads', 'TargetTitle2') IS NULL
    ALTER TABLE crm.CrmLeads ADD TargetTitle2 nvarchar(120) NULL;
IF COL_LENGTH('crm.CrmLeads', 'TargetTitle3') IS NULL
    ALTER TABLE crm.CrmLeads ADD TargetTitle3 nvarchar(120) NULL;
GO

-- ---------------------------------------------------------------------------
-- 3. GapScore polarity -- READ THIS BEFORE RUNNING IT
-- ---------------------------------------------------------------------------
-- GapScore now reads 100 = clean, 0 = worst. Anything reading these tables
-- must use:
--
--     a lead   is  GapScore < 100      (NOT > 0)
--     severe   is  GapScore <= 50      (NOT >= 50)
--
-- A dashboard still filtering "> 0" selects the compliant companies and
-- excludes every real lead.
--
-- The statement below converts rows written on the OLD scale. It is correct
-- exactly once -- run it twice and every score is silently back where it
-- started, with nothing in the data to show which way round the table is. It
-- is left commented deliberately. Skip it entirely for a fresh production
-- database, which has no old-scale rows.
--
-- UPDATE dbo.CRM_Scrapper SET GapScore = 100 - GapScore WHERE GapScore IS NOT NULL;
-- UPDATE crm.CrmLeads     SET GapScore = 100 - GapScore WHERE GapScore IS NOT NULL;
