"""
Stage 7 (contact ranking) configuration: title patterns, the two scales, company-size priority
lists, the relevance threshold and the model server. Everything a person might want to tune
lives here; titles.py, title_llm.py and ranking.py hold no thresholds of their own.

Scales (higher = more of it)

  seniority 1-6
    1  intern, trainee
    2  executive, associate, analyst, officer, engineer, coordinator   ("Executive" is junior in India)
    3  manager, senior manager, team lead
    4  AVP / assistant VP, deputy GM, associate/assistant director, statutory compliance officer
    5  VP, GM, director, head of a function, company secretary, DPO
    6  C-suite, founder, owner, proprietor, managing director, president, chairman

  relevance 0-5   how likely this person is to own or sign off consent management / data privacy for
                  the company. For rule matches it is  FUNCTION_BASE_RELEVANCE - SENIORITY_PENALTY.
                  For model-classified titles it is the model's own "r".
"""
import re

import config

from .settings import _env

# Bumped whenever the rules change (rule rows in ae_title_cache carry it) / whenever the system
# prompt, schema or few-shot examples change (model rows in ae_title_cache carry it, and a row from
# another version is never reused).
RULES_VERSION = "r1"
PROMPT_VERSION = "p1"

# --- the title classifier: which provider ----------------------------------------------------------
# AE_TITLE_PROVIDER = openai (the default) or gignati (the self-hosted llama-server, kept as the
# fallback). The OpenAI key is read from the environment variable OPENAI_API_KEY and nowhere else:
# it is not a setting, is never stored and never logged.
PROVIDERS = ("openai", "gignati")
TITLE_PROVIDER = _env("AE_TITLE_PROVIDER", "openai")

OPENAI_BASE_URL = _env("AE_OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = _env("AE_OPENAI_TITLE_MODEL", "gpt-4.1-nano")
OPENAI_TIMEOUT_S = _env("AE_OPENAI_TIMEOUT_S", 30, float)

# gignati: the self-hosted llama-server. The name is only the label stored with its cached answers.
LLM_BASE_URL = _env("AE_LLM_BASE_URL", config.LLM_BASE_URL)
GIGNATI_MODEL = _env("AE_GIGNATI_MODEL", "llama-server")
LLM_TIMEOUT_S = _env("AE_LLM_TIMEOUT_S", 30, float)

LLM_MAX_TOKENS = 40                   # both providers: the answer is about 20 tokens of compact JSON
LLM_ATTEMPTS = 2                      # one try and one retry for invalid output


def active_provider():
    """(provider, model) the classifier is configured for; an unknown provider is an error."""
    if TITLE_PROVIDER not in PROVIDERS:
        raise ValueError(f"AE_TITLE_PROVIDER={TITLE_PROVIDER!r} is not one of {PROVIDERS}")
    return (TITLE_PROVIDER, OPENAI_MODEL if TITLE_PROVIDER == "openai" else GIGNATI_MODEL)

# --- the ranking ---------------------------------------------------------------------------------
# The best contact must reach this relevance, or the company is held_no_contact. 2 admits an IT or
# marketing executive but not HR, sales, finance managers or interns.
RELEVANCE_MIN = _env("AE_RELEVANCE_MIN", 2, int)

FUNCTIONS = ("founder", "ceo", "director", "tech", "marketing", "dpo", "legal", "security",
             "hr", "sales", "finance", "operations", "other")

# What a function is worth at a senior level; seniority takes points off it.
FUNCTION_BASE_RELEVANCE = {
    "founder": 5, "ceo": 5, "dpo": 5, "legal": 5, "security": 5, "tech": 5, "marketing": 5,
    "director": 4,          # a director with no stated department
    "operations": 3, "finance": 2, "hr": 1, "sales": 1, "other": 1,
}
SENIORITY_PENALTY = {6: 0, 5: 0, 4: 1, 3: 2, 2: 3, 1: 4}
DEFAULT_SENIORITY = 2                 # a function matched but no level word: treat as junior

# --- company size -----------------------------------------------------------------------------------
# CRM CompanyScale (lowercased, spaces removed) -> small / mid / large. Anything else, or no scale at
# all, uses the one blended list.
SCALE_TO_SIZE = {
    "msme": "small", "startup": "small", "1-50": "small", "51-100": "small",
    "smb": "mid", "101-250": "mid", "251-500": "mid",
    "enterprise": "large", "501-1000": "large", "1001-2500": "large",
    "2501-5000": "large", "5001&above": "large",
}
# Functions in the order to prefer them among contacts of equal relevance.
PRIORITY_LISTS = {
    "small": ("founder", "ceo", "director"),
    "mid": ("marketing", "tech"),
    "large": ("dpo", "legal", "security", "marketing"),
    "blended": ("founder", "ceo", "dpo", "legal", "security", "marketing", "tech", "director"),
}


def size_class(scale):
    key = re.sub(r"\s+", "", (scale or "").lower())
    return SCALE_TO_SIZE.get(key, "blended")


# "president" but not "vice / assistant vice / associate vice / senior vice / executive vice president".
_PRESIDENT = (r"(?<!vice )(?<!assistant vice )(?<!associate vice )(?<!senior vice )"
              r"(?<!executive vice )\bpresident\b")

# --- seniority patterns -------------------------------------------------------------------------------
# Checked top to bottom on the NORMALISED title (lowercase, punctuation to spaces); the first hit wins.
# 4 sits before 5 so "assistant director" and "AVP" are not read as director / VP.
SENIORITY_RULES = (
    (6, r"^(ceo|cto|cio|cmo|ciso|coo|cfo|cpo|cdo|cco|cro|cgo|md)$|\bchief\b|\bfounders?\b|\bco ?founder\b|"
        r"\bowner\b|\bproprietor\b|managing director|" + _PRESIDENT + r"|\bchairman\b|\bchairperson\b|"
        r"\bpromoter\b|managing partner|founding partner|^(senior |equity )?partner$|\bceo\b|\bmd\b"),
    (4, r"\bavp\b|(assistant|associate) vice president|(associate|assistant|deputy|joint) director|"
        r"(deputy|assistant|associate) general manager|\bdgm\b|\bagm\b|compliance officer"),
    (5, r"vice president|\bsvp\b|\bevp\b|(?<!a)\bvp\b|general manager|\bgm\b|\bdirector\b|\bhead\b|"
        r"company secretary|data protection officer|\bdpo\b|grievance officer|privacy officer|"
        r"information security officer|general counsel"),
    (3, r"\bmanager\b|\bsupervisor\b|team lead|\blead\b|senior manager"),
    (2, r"\bexecutive\b|\bassociate\b|\banalyst\b|\bofficer\b|\bcoordinator\b|\bspecialist\b|\bengineer\b|"
        r"\bdeveloper\b|\bconsultant\b|\badministrator\b|\bassistant\b|\brepresentative\b|\bprogrammer\b|"
        r"\bsde\b|\bexec\b"),
    (1, r"\bintern\b|\binternship\b|\btrainee\b|\bapprentice\b|\bfresher\b|\bstudent\b|\bvolunteer\b"),
)

# --- function patterns ---------------------------------------------------------------------------------
# (function, regex, label). First hit wins, so more specific functions come first. Founder and CEO
# first, so "Founder & CTO" is a founder; marketing before tech, so "Head of Digital" is marketing.
TITLE_RULES = (
    ("founder", r"\bco ?founder\b|\bfounders?\b|\bproprietor\b|^(co |joint )?owner$|^owner (and|cum)\b|"
               r"\b(and|cum) owner$|^(business|company|firm|shop|factory|hotel) owner$|"
               r"^(managing |senior |founding |equity )?partner$|^partner (and|cum)\b|managing partner|"
               r"founding partner|\bpromoter\b|\bchairman\b|\bchairperson\b",
     "founder / owner / proprietor / partner"),
    ("ceo", r"\bceo\b|chief executive|managing director|\bmd\b|executive chairman|"
            r"" + _PRESIDENT, "CEO / managing director / president"),
    ("dpo", r"data protection officer|\bdpo\b|chief privacy|\bcpo\b|privacy (officer|head|lead|manager|counsel)|"
            r"grievance officer", "data protection / privacy officer"),
    ("security", r"\bciso\b|information security|cyber ?security|chief security|infosec|"
                 r"security (head|officer|manager|lead|engineer|analyst)|head of security",
     "information security"),
    ("legal", r"legal|compliance|general counsel|\bcounsel\b|company secretary|regulatory|governance|"
              r"\bgrc\b|advocate|lawyer", "legal / compliance / secretarial"),
    ("marketing", r"\bcmo\b|chief (marketing|digital|growth|brand|communications?) officer|marketing|"
                  r"\bdigital\b|\bbrand\b|\bgrowth\b|communications?\b|\bpr\b|public relations|\bseo\b|"
                  r"social media|advertising|e ?commerce", "marketing / digital / brand"),
    ("tech", r"\bcto\b|\bcio\b|chief (technology|technical|information|data) officer|information technology|"
             r"\bit\b|technology (head|lead|manager|director)|(head|vp|vice president|director|manager|lead) "
             r"(of )?(technology|engineering)|technical (director|head|lead|manager)|software "
             r"(engineer|developer|architect|head|manager|lead|director)|devops|web developer|"
             r"infrastructure|system administrator|sysadmin|\berp\b", "technology / IT"),
    ("hr", r"\bhr\b|human resources?|talent|recruit|people (operations|ops|partner|and culture)|payroll|"
           r"learning and development", "human resources"),
    ("sales", r"sales|business development|\bbd\b|\bbde\b|\bbdm\b|account (manager|executive)|key account|"
              r"\bchannel\b|relationship manager|customer success|inside sales|revenue", "sales / business development"),
    ("finance", r"finance|financial|\baccounts?\b|accounting|\bcfo\b|treasury|\baudit\b|controller|"
                r"\btax(ation)?\b", "finance / accounts"),
    ("operations", r"operations?|\bcoo\b|\bops\b|supply chain|logistics|procurement|purchase|production|"
                   r"\bplant\b|factory|manufacturing|administration|facilities|quality|"
                   r"project (manager|head|lead|director)|delivery", "operations"),
    # A director, VP or GM with no department named: the last resort, so "Director - IT" is tech.
    ("director", r"^((senior|executive|associate|assistant|deputy|joint|whole time|technical|managing) )*"
                 r"(director|general manager|gm|vice president|vp|svp|evp|avp)$|^whole time director",
     "director / VP / GM, no department stated"),
)
