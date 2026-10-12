"""
Config for stage 9 (drafting). Every value can be overridden by an AE_* environment variable; the key is
read from the environment only, at call time, and is never stored or logged.

The wording rules (banned words, length limits, legal-claim patterns) are the safety net around the two
sentences the model writes; the fixed parts of each email live in templates/email/.
"""
from pathlib import Path

import config

from .settings import _env

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates" / "email"

# --- provider ------------------------------------------------------------------------------------
PROVIDERS = ("openai", "gignati", "mock")
PROVIDER = _env("AE_DRAFT_PROVIDER", "openai")
if PROVIDER not in PROVIDERS:
    raise ValueError(f"AE_DRAFT_PROVIDER must be one of {PROVIDERS}, not {PROVIDER!r}")
OPENAI_BASE_URL = _env("AE_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = _env("AE_OPENAI_DRAFT_MODEL", "gpt-4.1-mini")
OPENAI_KEY_ENV = "OPENAI_API_KEY"
# The self-hosted server (a Qwen thinking model): the fallback provider. Its address is the same
# AE_LLM_BASE_URL the title classifier uses.
GIGNATI_BASE_URL = _env("AE_LLM_BASE_URL", config.LLM_BASE_URL).rstrip("/")
GIGNATI_MODEL = _env("AE_GIGNATI_DRAFT_MODEL", "")

TEMPERATURE = _env("AE_DRAFT_TEMPERATURE", 0.3, float)
MAX_TOKENS = _env("AE_DRAFT_MAX_TOKENS", 200, int)
TIMEOUT_S = _env("AE_DRAFT_TIMEOUT_S", 30, float)
HTTP_RETRIES = _env("AE_DRAFT_HTTP_RETRIES", 2, int)       # for 429 / 5xx / timeouts, on top of the one regeneration
RETRY_WAIT_S = _env("AE_DRAFT_RETRY_WAIT_S", 2, float)
PROMPT_VERSION = "d1"

# ----------------------------------------------------------------------------------------------------
# UNVERIFIED PRICES. What a call costs, USD per million tokens, for the cost ESTIMATE only. These are placeholder
# defaults that nobody has checked against OpenAI's price list: set AE_DRAFT_PRICE_PER_M_INPUT and
# AE_DRAFT_PRICE_PER_M_OUTPUT to the current prices for the configured model, then AE_DRAFT_PRICES_VERIFIED=1.
# ----------------------------------------------------------------------------------------------------
PRICES_VERIFIED = _env("AE_DRAFT_PRICES_VERIFIED", False, lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"))
PRICE_PER_M_INPUT = _env("AE_DRAFT_PRICE_PER_M_INPUT", 0.40, float)
PRICE_PER_M_OUTPUT = _env("AE_DRAFT_PRICE_PER_M_OUTPUT", 1.60, float)


def model_for(provider):
    return {"openai": OPENAI_MODEL, "gignati": GIGNATI_MODEL or "gignati-default", "mock": "mock"}[provider]


# --- validation ---------------------------------------------------------------------------------------
LENGTH_LIMITS = {"opening_line": (30, 170), "finding_sentence": (40, 240)}      # characters, inclusive

BANNED_WORDS = tuple(w.strip() for w in _env(
    "AE_DRAFT_BANNED_WORDS",
    "violation,violations,fine,fines,fined,penalty,penalties,deadline,deadlines,illegal,illegally,"
    "lawsuit,lawsuits,sue,sued,non-compliant,noncompliant,non-compliance,unlawful,liable,liability,"
    "breach,breaches,prosecution,court,enforcement,mandatory,urgent,urgently,act now,last chance").split(",")
    if w.strip())

# Anything that reads as a statement about the law. The report maps findings to DPDP sections; an email
# sentence written by a model must not.
LEGAL_PATTERNS = (
    r"\b(?:by|under)\s+(?:the\s+)?(?:law|act|regulation|dpdp)\b",
    r"\brequired\s+(?:by|to)\b",
    r"\b(?:law|laws|legal|legally|regulation|regulations|regulator|regulators|statute|dpdp|compliance|complies|comply)\b",
    r"\bsection\s+\d",
)

# Product and vendor names to watch for besides the ones in report_catalogue.yaml. A name in a slot must
# be one of the company's own findings.
EXTRA_NAMES = ("Shopify", "WordPress", "WooCommerce", "Wix", "Squarespace", "Razorpay", "Salesforce",
               "Zoho", "Mailchimp", "Cloudflare", "Facebook", "Instagram", "Twitter", "TikTok",
               "Pinterest", "Snapchat", "Hubspot", "Intercom", "Drift", "Zendesk", "Freshworks",
               "Cookiebot", "OneTrust", "CookieYes", "Usercentrics")
# Names that are also everyday words: matched case-sensitively so "segment" the noun passes.
COMMON_WORD_NAMES = ("Segment", "Amplitude", "Drift", "Klaro", "Ketch")

# Words for numbers up to twenty and the tens; "one" is left out on purpose (it is also a pronoun).
NUMBER_WORDS = {w: i for i, w in enumerate(
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split(), start=2)}
NUMBER_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                     "ninety": 90, "hundred": 100, "dozen": 12, "zero": 0})
