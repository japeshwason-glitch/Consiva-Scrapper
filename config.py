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
LUSHA_API_KEY = os.environ.get("LUSHA_API_KEY")
APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN")

# Local llama.cpp server used for soft-field extraction in stage 03.
# Self-hosted on the LAN: no company data leaves the network, and inference is
# free, so it can run on every page rather than being rationed.
# It is never used for CMP detection or business-scale classification -- see
# the module docstring in stages/03_scrape_playwright.py for why.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://gemma4-server.gignati.com")
LLM_ENABLED = os.environ.get("LLM_ENABLED", "1") not in ("0", "false", "False", "")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
