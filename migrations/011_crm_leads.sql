-- 011_crm_leads.sql -- Auto Email Campaign: leads from crm.CrmLeads
--
-- crm.CrmLeads rows have a GUID Id, but ae_companies is keyed by an integer. Each lead gets a stable integer
-- company_id from this sequence (above 1,000,000,000, so it can never collide with a dbo.CRM_Scrapper Id), and
-- ae_lead_map remembers which GUID it is. Nothing here touches the CRM.
CREATE SEQUENCE ae_lead_company_id_seq START 1000000001;
CREATE TABLE ae_lead_map (
    lead_id     TEXT PRIMARY KEY,                                   -- crm.CrmLeads.Id, lower-case
    company_id  INTEGER NOT NULL UNIQUE DEFAULT nextval('ae_lead_company_id_seq'),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
