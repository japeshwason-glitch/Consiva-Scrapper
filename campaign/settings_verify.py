"""
Stage 8 (NeverBounce) configuration. Thresholds shared with the rest of the campaign come
from settings.py; the NeverBounce-specific ones live here. Each can be overridden by an AE_*
environment variable. The API key is read from the ENVIRONMENT only (NEVERBOUNCE_API_KEY), at
call time: it is never a setting, never written to a file, never logged.
"""
import json
import os

from . import settings
from .settings import _env

# off  = the stage does nothing (companies stay contact_ranked)
# mock = verdicts come from MOCK_VERDICTS / MOCK_DEFAULT, nothing leaves this machine, no cap is spent
# live = the real API, with NEVERBOUNCE_API_KEY
MODE = _env("AE_NB_MODE", "mock")
MODES = ("off", "mock", "live")

API_BASE = _env("AE_NB_API_BASE", "https://api.neverbounce.com/v4")
HTTP_TIMEOUT_S = _env("AE_NB_HTTP_TIMEOUT_S", 20, int)

DAILY_CREDIT_CAP = settings.NEVERBOUNCE_DAILY_CREDIT_CAP    # credits per IST calendar day
CACHE_DAYS = settings.VERIFICATION_CACHE_DAYS               # a verdict this young is reused, not paid for again
REPORT_FRESH_DAYS = settings.REPORT_FRESH_DAYS              # the report must be younger than this
FALLBACKS = settings.FALLBACK_CONTACTS                      # contacts checked after the top one
DEFAULT_BATCH = _env("AE_NB_BATCH", 500, int)               # companies claimed per run when no --limit

# Mock mode: a JSON object mapping an address OR a domain to a NeverBounce verdict
# ("valid" | "invalid" | "disposable" | "catchall" | "unknown"); anything else gets MOCK_DEFAULT.
MOCK_VERDICTS = json.loads(_env("AE_NB_MOCK_VERDICTS", "{}"))
MOCK_DEFAULT = _env("AE_NB_MOCK_DEFAULT", "valid")


def api_key():
    """The NeverBounce key from the environment, or None."""
    return os.environ.get("NEVERBOUNCE_API_KEY") or None
