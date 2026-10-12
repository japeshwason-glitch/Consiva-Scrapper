"""
Classify a job title with a language model. A NEW function: llm.py is left as it is.

    classify_title("Strategy Consultant") -> {"f": "other", "s": 2, "r": 1}    valid answer
                                          -> None                               invalid twice: unclassified
                                          raises ServerUnavailable               unreachable / HTTP error
                                          raises ProviderNotConfigured           no OPENAI_API_KEY (a ServerUnavailable)

Two providers, chosen by settings_titles.TITLE_PROVIDER (env AE_TITLE_PROVIDER):
  openai   (default) POST {AE_OPENAI_BASE_URL}/chat/completions, Authorization: Bearer $OPENAI_API_KEY,
           model AE_OPENAI_TITLE_MODEL, Structured Outputs (json_schema, strict), temperature 0,
           max_completion_tokens 40.
  gignati  the self-hosted llama-server, POST {AE_LLM_BASE_URL}/v1/chat/completions: json_schema,
           temperature 0, max_tokens 40, thinking switched off (the Qwen3.6 server otherwise spends
           every token on hidden reasoning and returns nothing).
Both get the same system prompt, a module constant, byte-identical on every call (so prefix caching works
where there is any), and a user message that is ONLY the job title: no name, email, company or domain
ever leaves. The OpenAI key comes from the environment alone; it is sent as the Bearer header and is
never put in a body, a log line, an error message or the database. Every reply is validated; invalid output is retried
once, then given up on. A server that cannot be reached is a different thing from a title that
cannot be classified, and is reported differently, so the caller never mistakes one for the other.
"""
import json
import os
import urllib.error
import urllib.request

import llm

from . import settings_titles as st


class ServerUnavailable(Exception):
    """Connection error, timeout, HTTP error or a non-JSON reply: nothing was learned about the title."""


class ProviderNotConfigured(ServerUnavailable):
    """The provider cannot be used at all yet (no OPENAI_API_KEY). No request was made."""


SCHEMA = {
    "type": "object",
    "properties": {
        "f": {"type": "string", "enum": list(st.FUNCTIONS)},
        "s": {"type": "integer", "enum": [1, 2, 3, 4, 5, 6]},
        "r": {"type": "integer", "enum": [0, 1, 2, 3, 4, 5]},
    },
    "required": ["f", "s", "r"],
    "additionalProperties": False,
}

# Constant on purpose: never format a title, a date or a count into it.
SYSTEM_PROMPT = """You classify one job title from an Indian company. The reader is selling consent management and data-privacy compliance (India's DPDP Act), so you judge who at the company would own or approve that.
Reply with compact JSON on one line, nothing else: {"f":FUNCTION,"s":SENIORITY,"r":RELEVANCE}

f, the function, exactly one of: founder, ceo, director, tech, marketing, dpo, legal, security, hr, sales, finance, operations, other.
  founder = founder, co-founder, owner, proprietor, partner of a small firm. ceo = CEO, managing director, president.
  director = a director, VP or GM with no department stated. tech = IT, engineering, CTO, CIO. marketing = marketing, digital, brand, growth, communications.
  dpo = data protection or privacy officer. legal = legal, compliance, company secretary, general counsel. security = information security, CISO.
  hr = human resources. sales = sales, business development. finance = finance, accounts. operations = operations, production, supply chain.
  other = anything else, including consultants, analysts, product, customer support and admin.
s, seniority: 1 intern or trainee. 2 executive, associate, analyst, officer, engineer. 3 manager, senior manager, team lead. 4 AVP, deputy GM, associate director. 5 VP, GM, director, head of a function. 6 C-suite, founder, owner, managing director.
r, relevance 0 to 5: how likely this person owns or signs off consent or privacy compliance. 5 = the decision maker (founder, CEO, DPO, head of legal, CISO, CTO, head of marketing). 3 = a manager in those areas. 2 = a junior in those areas. 1 or 0 = HR, sales, finance, interns, unrelated roles.

Indian title conventions: "Executive" is junior (2), never senior. AVP is below VP. GM varies by company: a GM with a department takes that department. A bare "Partner", "Proprietor" or "Owner" runs the firm (founder, 6, 5). "Sr." is Senior. "Manager - IT" is a tech manager (3). "Director - Operations" is a senior operations role. An "Associate" is junior. "Consultant" is outside the company's management (other).

Examples:
Proprietor -> {"f":"founder","s":6,"r":5}
Partner -> {"f":"founder","s":6,"r":5}
Executive - Sales -> {"f":"sales","s":2,"r":0}
AVP - Digital Marketing -> {"f":"marketing","s":4,"r":4}
GM - IT -> {"f":"tech","s":5,"r":5}
General Manager -> {"f":"director","s":5,"r":4}
Director - Operations -> {"f":"operations","s":5,"r":3}
Sr. Manager - HR -> {"f":"hr","s":3,"r":0}
Manager - IT -> {"f":"tech","s":3,"r":3}
Compliance Executive -> {"f":"legal","s":2,"r":2}
Head of Information Security -> {"f":"security","s":5,"r":5}
Business Analyst -> {"f":"other","s":2,"r":1}
Customer Support Lead -> {"f":"other","s":3,"r":1}
Management Trainee -> {"f":"other","s":1,"r":0}
Executive Assistant to MD -> {"f":"other","s":2,"r":1}"""


def build_body(title, provider=None):
    """The request body for a provider; the system prompt is the module constant and the title is the only variable part."""
    provider = provider or st.active_provider()[0]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": title}]
    response_format = {"type": "json_schema",
                       "json_schema": {"name": "title_classification", "strict": True, "schema": SCHEMA}}
    if provider == "openai":
        return {"model": st.OPENAI_MODEL, "messages": messages, "temperature": 0,
                "max_completion_tokens": st.LLM_MAX_TOKENS, "response_format": response_format}
    return {"messages": messages, "temperature": 0, "max_tokens": st.LLM_MAX_TOKENS,
            "response_format": response_format, "chat_template_kwargs": {"enable_thinking": False}}


def _openai_key():
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ProviderNotConfigured("OPENAI_API_KEY is not set")
    return key


def _post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:   # HTTPError is a URLError
        # Only the exception's type and message: never the request, so a key in a header cannot leak.
        raise ServerUnavailable(f"{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw)["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ServerUnavailable(f"reply is not a chat completion: {raw[:80]!r}") from exc


def parse_reply(content):
    """{"f","s","r"} if `content` is exactly a valid answer, else None."""
    try:
        out = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(out, dict) or set(out) != {"f", "s", "r"}:
        return None
    f, s, r = out["f"], out["s"], out["r"]
    if f not in st.FUNCTIONS:
        return None
    for v, lo, hi in ((s, 1, 6), (r, 0, 5)):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return None
    return {"f": f, "s": s, "r": r}


def classify_title(title, base_url=None, timeout=None, provider=None):
    """See the module docstring. `provider`, `base_url` and `timeout` default to the settings, read at call time."""
    provider = provider or st.active_provider()[0]
    if provider not in st.PROVIDERS:
        raise ValueError(f"unknown title provider {provider!r}")
    title = " ".join((title or "").split())[:120]
    if provider == "openai":
        url = f"{(base_url or st.OPENAI_BASE_URL).rstrip('/')}/chat/completions"
        timeout = timeout or st.OPENAI_TIMEOUT_S
        headers = {"Content-Type": "application/json", "User-Agent": "consiva-campaign/1",
                   "Authorization": f"Bearer {_openai_key()}"}
    else:
        url = f"{(base_url or st.LLM_BASE_URL).rstrip('/')}/v1/chat/completions"
        timeout = timeout or st.LLM_TIMEOUT_S
        headers = llm._HEADERS
    for _ in range(st.LLM_ATTEMPTS):
        reply = parse_reply(_post(url, headers, build_body(title, provider), timeout))
        if reply is not None:
            return reply
    return None
