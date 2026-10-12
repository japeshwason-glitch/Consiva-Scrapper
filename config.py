"""
Central place for connection strings and API keys -- loaded from environment
variables, never hardcoded. Every stage script imports from here instead of
reading os.environ directly.

A .env file sitting next to this module is loaded on import as a convenience
for cron/local runs. Real environment variables always win, so an exported
DATABASE_URL overrides whatever .env says.
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def _load_dotenv(path=ENV_PATH):
    """Minimal KEY=VALUE reader -- avoids taking a dependency just for this."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)  # never clobber a real env var


_load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")  # postgres connection string
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY")
# Optional pool, comma-separated. Each key carries its own 50/min, 200/hour and
# 600/day allowance, so N keys raise the daily contact ceiling to N x 600.
# Falls back to the single key when unset.
APOLLO_API_KEYS = [k.strip() for k in
                   (os.environ.get("APOLLO_API_KEYS") or APOLLO_API_KEY or "").split(",")
                   if k.strip()]
LUSHA_API_KEY = os.environ.get("LUSHA_API_KEY")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

# Both accounts are on free tiers. Stage 06 may use Apollo's people SEARCH
# (free) to identify the right persona, but must NOT call people/match with
# reveal_personal_emails -- that consumes paid credits this account does not
# have. Flip when the account is upgraded.
# CRM handoff target — the sales team's SQL Server, not ours.
CRM_DB_HOST = os.environ.get("CRM_DB_HOST")
CRM_DB_PORT = int(os.environ.get("CRM_DB_PORT") or 1433)
CRM_DB_NAME = os.environ.get("CRM_DB_NAME")
CRM_DB_USER = os.environ.get("CRM_DB_USER")
CRM_DB_PASSWORD = os.environ.get("CRM_DB_PASSWORD")
CRM_DB_TABLE = os.environ.get("CRM_DB_TABLE", "dbo.CRM_Scrapper")
# The inbound job's target. Schema-qualified, and configurable for the same
# reason as the table above: staging and production need not agree on naming.
CRM_LEADS_TABLE = os.environ.get("CRM_LEADS_TABLE", "crm.CrmLeads")
# The two jobs can target different DATABASES on the same server: the MCA
# scraper may still be pointed at staging while inbound leads, which are real
# people who filled in a form, go to the live one. Falls back to CRM_DB_NAME
# when unset, so a single-database setup needs no extra variable.
CRM_LEADS_DB_NAME = os.environ.get("CRM_LEADS_DB_NAME") or CRM_DB_NAME

# One-page DPDP reports (report_build.py, docs/REPORTS.md). PDFs go to a
# Cloudflare R2 bucket served on REPORT_PUBLIC_BASE_URL, where the URL path
# equals the object key.
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_REPORTS_BUCKET = os.environ.get("R2_REPORTS_BUCKET", "consiva-reports")
R2_ENDPOINT = (f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
               if R2_ACCOUNT_ID else None)
REPORT_PUBLIC_BASE_URL = (os.environ.get("REPORT_PUBLIC_BASE_URL")
                          or "https://reports-consiva.gignati.com").rstrip("/")
REPORT_SALES_CONTACT = os.environ.get("REPORT_SALES_CONTACT", "")

APOLLO_ALLOW_EMAIL_REVEAL =os.environ.get("APOLLO_ALLOW_EMAIL_REVEAL", "0") not in ("0", "false", "False", "")

# Local llama.cpp server used for soft-field extraction in stage 03.
# Self-hosted on the LAN: no company data leaves the network, and inference is
# free, so it can run on every page rather than being rationed.
# It is never used for CMP detection or business-scale classification -- see
# the module docstring in stages/03_scrape_playwright.py for why.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://gemma4-server.gignati.com")
LLM_ENABLED = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "False", "")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
