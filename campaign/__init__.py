"""
Auto Email Campaign pipeline (docs/report_pipeline.md, docs/crm_schema.md).

Drafts one first email per PartnerImport company. It never sends: the CRM team
sends. The CRM is read-only here (crm_read.py); everything else is local Postgres.
"""
