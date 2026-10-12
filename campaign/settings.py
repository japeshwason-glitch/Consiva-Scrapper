"""
Every threshold and name the campaign jobs use, in one place. Each can be
overridden by an AE_* environment variable; the defaults are the agreed ones.
"""
import os


def _env(name, default, cast=str):
    raw = os.environ.get(name)
    return default if raw in (None, "") else cast(raw)


def _bool(raw):
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# --- who is in scope -------------------------------------------------------------
ALLOWED_SOURCES = tuple(s.strip() for s in _env("AE_ALLOWED_SOURCES", "PartnerImport").split(","))

# Production intake takes every PartnerImport company not yet in ae_companies. Those
# that already carry a ReportStatus were processed by the retired report script and
# are imported once as `legacy`. This pipeline owns ScrapeStatus and ReportStatus now.
EXCLUDE_PREVIOUSLY_EMAILED = _env("AE_EXCLUDE_PREVIOUSLY_EMAILED", True, _bool)

# --- scoring ----------------------------------------------------------------------
# Inclusive (low, high). Band B ends at 50 to match the report's "priority at 50
# or below" label (fingerprints.SEVERE_AT).
BAND_CUTOFFS = {"A": (0, 24), "B": (25, 50), "C": (51, 74), "D": (75, 90)}
SKIP_ABOVE_SCORE = _env("AE_SKIP_ABOVE_SCORE", 90, int)
MIN_GAPS = _env("AE_MIN_GAPS", 1, int)

# --- freshness and caches -----------------------------------------------------------
REPORT_FRESH_DAYS = _env("AE_REPORT_FRESH_DAYS", 14, int)
VERIFICATION_CACHE_DAYS = _env("AE_VERIFICATION_CACHE_DAYS", 45, int)
DRAFT_EXPIRY_DAYS = _env("AE_DRAFT_EXPIRY_DAYS", 14, int)

# --- volumes ----------------------------------------------------------------------
NEVERBOUNCE_DAILY_CREDIT_CAP = _env("AE_NEVERBOUNCE_DAILY_CREDIT_CAP", 200, int)
DRAFTS_PER_DAY = _env("AE_DRAFTS_PER_DAY", 100, int)
FALLBACK_CONTACTS = _env("AE_FALLBACK_CONTACTS", 2, int)
SCAN_RETRIES = _env("AE_SCAN_RETRIES", 2, int)            # attempts = retries + 1
SCAN_RETRY_DELAY_MINUTES = _env("AE_SCAN_RETRY_DELAY_MINUTES", 60, int)
INTAKE_BATCH_SIZE = _env("AE_INTAKE_BATCH_SIZE", 500, int)

# Production companies are NOT processed by stages 2-8 until this is switched on (AE_PRODUCTION_ENABLED=1)
# or a run is started by hand with --production. The scheduled job only takes companies in; the test
# cohort (--sample) always runs.
PRODUCTION_ENABLED = _env("AE_PRODUCTION_ENABLED", False, _bool)

# --- stage 4: the scan ------------------------------------------------------------------
SCAN_REUSE_DAYS = REPORT_FRESH_DAYS                         # a scan younger than this is reused, not repeated
SCAN_CONCURRENCY = _env("AE_SCAN_CONCURRENCY", 6, int)
SCAN_TIMEOUT_S = _env("AE_SCAN_TIMEOUT_S", 150, int)        # one homepage, killed hard past this
SCANS_PER_RUN = _env("AE_SCANS_PER_RUN", 60, int)           # bounded work per run
SCAN_RETRY_BACKOFF_MINUTES = (15, 60)                       # wait before retry 1, retry 2
RENDER_TIMEOUT_S = _env("AE_RENDER_TIMEOUT_S", 60, int)

# --- email address checks -----------------------------------------------------------
FREE_MAIL_DOMAINS = frozenset(d.strip() for d in _env(
    "AE_FREE_MAIL_DOMAINS",
    "gmail.com,googlemail.com,yahoo.com,yahoo.in,yahoo.co.in,ymail.com,outlook.com,"
    "hotmail.com,live.com,msn.com,rediffmail.com,rediff.com,aol.com,icloud.com,"
    "protonmail.com,proton.me").split(",") if d.strip())

# --- the CRM (read-only in this milestone) -------------------------------------------
# Table and column names, so staging and production need not agree on naming.
CRM = {
    "companies": _env("AE_CRM_COMPANIES_TABLE", "dbo.CRM_Scrapper"),
    "contacts": _env("AE_CRM_CONTACTS_TABLE", "crm.CrmCompanyContacts"),
    "activities": _env("AE_CRM_ACTIVITIES_TABLE", "crm.CrmCompanyActivities"),
    "company_events": _env("AE_CRM_COMPANY_EVENTS_TABLE", "crm.CrmCompanyEmailEvents"),
    "campaign_events": _env("AE_CRM_CAMPAIGN_EVENTS_TABLE", "crm.CrmEmailEvents"),
    "company_id": "Id", "source": "Source", "website": "Website", "company_scale": "CompanyScale", "company_name": "CompanyName", "industry": "Industry",
    "report_url": "DownloadReportUrl", "gap_score": "GapScore", "report_generated": "ReportGeneratedAt",
    "scrape_status": "ScrapeStatus", "report_status": "ReportStatus",
    "contact_company_id": "CompanyId", "contact_id": "Id", "contact_email": "Email",
    "contact_title": "JobTitle", "contact_first_name": "FirstName", "contact_deleted": "IsDeleted",
    "activity_company_id": "CompanyId", "activity_type": "Type", "activity_deleted": "IsDeleted",
    "event_type": "EventType", "event_email": "RecipientEmail",
}
EMAIL_ACTIVITY_TYPE = "Email"
BOUNCE_EVENT_TYPE = "Bounce"

# --- what this pipeline may write to the CRM (campaign/crm_write.py) --------------------
# Allowed by name, nothing else, ever, PartnerImport rows only:
#   * the two status columns, with only the values the table already uses;
#   * the report columns the retired report job used to fill (see crm_write.REPORT_COLUMNS);
#   * the AE_ draft columns the CRM team created.
AE_COLUMNS = ("AE_DraftBand", "AE_DraftBody", "AE_DraftContactId", "AE_DraftEmail",
              "AE_DraftGeneratedAt", "AE_DraftSentAt", "AE_DraftStatus", "AE_DraftSubject",
              "AE_DraftVersion")
VOCAB = {
    "ScrapeStatus": frozenset({"done", "failed"}),
    "ReportStatus": frozenset({"generated", "no_gap", "unscannable"}),
}
WRITE_SOURCE = "PartnerImport"      # fixed, not configurable: the guard must not be loosened by env

# crm_write refuses EVERY CRM write unless the Postgres database it is connected to is this one. A throwaway,
# test or demo database must never be able to drive a real CRM write.
PRIMARY_DATABASE = _env("AE_PRIMARY_DATABASE", "consiva")

# A draft built from a template whose version still says "placeholder" must not reach the CRM as sendable
# (AE_DraftStatus ready / risky). With this switched on (demo only) it is written as 'expired' so the CRM
# team can see it and cannot send it by mistake.
ALLOW_PLACEHOLDER_AE_WRITE = _env("AE_ALLOW_PLACEHOLDER_AE_WRITE", False, _bool)
