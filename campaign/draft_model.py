"""
The model call for the two draft slots, and what the model is allowed to see.

The model gets company facts and the structured findings, plus the contact's job FUNCTION ("ceo",
"marketing"): never a name, email, phone or the raw title. Code inserts the first name after generation.
Providers: openai (default; Structured Outputs, key from OPENAI_API_KEY), gignati (the self-hosted
fallback), mock (no network; deterministic, valid text, for tests and dry development).
"""
import json
import os
import time
import urllib.error
import urllib.request

import llm

from . import settings_drafts as cfg

SYSTEM_PROMPT = (
    "You write two sentences for a first outreach email from a data-protection software company to a "
    "business. Use ONLY the facts in the JSON the user sends. Never add or invent facts, numbers, vendor or "
    "product names, or claims about the company or its customers.\n"
    "opening_line: one sentence about the company's business, using only company_name and what_it_does "
    "(if what_it_does is null, write a neutral sentence about the company's website).\n"
    "finding_sentence: one sentence that frames the top finding in plain, calm language for the reader's "
    "role (contact_function). No legal references, no urgency, no threats, no advice on what is required.\n"
    "Each sentence ends with a full stop. No greetings, no names of people, no URLs, no email addresses, no "
    "markup. Reply with JSON only."
)
SCHEMA = {
    "type": "object",
    "properties": {"opening_line": {"type": "string"}, "finding_sentence": {"type": "string"}},
    "required": ["opening_line", "finding_sentence"],
    "additionalProperties": False,
}
_FINDING_FIELDS = ("top_finding", "finding_codes", "tracking_cookie_count", "third_party_count",
                   "tracker_vendors", "third_party_domains", "cookie_banner", "privacy_policy", "cmp", "form")


class ModelUnavailable(Exception):
    """The provider cannot be used right now (no key, refused, down, rate limited past our retries)."""


def build_payload(findings, contact_function):
    """The only data that leaves this machine for a draft."""
    return {"company_name": findings["company_name"], "domain": findings["domain"],
            "what_it_does": findings["what_it_does"], "band": findings["band"], "score": findings["score"],
            "gap_count": findings["gap_count"], "scan_date": findings["scan_date"],
            "findings": {k: findings[k] for k in _FINDING_FIELDS}, "contact_function": contact_function}


def build_request(provider, payload, key=None):
    """(url, headers, body) for one call. Pure, so tests can inspect exactly what would be sent."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]
    response_format = {"type": "json_schema",
                       "json_schema": {"name": "draft_slots", "strict": True, "schema": SCHEMA}}
    if provider == "openai":
        return (f"{cfg.OPENAI_BASE_URL}/chat/completions",
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                {"model": cfg.OPENAI_MODEL, "messages": messages, "temperature": cfg.TEMPERATURE,
                 "max_completion_tokens": cfg.MAX_TOKENS, "response_format": response_format})
    if provider == "gignati":
        body = {"messages": messages, "temperature": cfg.TEMPERATURE, "max_tokens": cfg.MAX_TOKENS,
                "response_format": response_format, "chat_template_kwargs": {"enable_thinking": False}}
        if cfg.GIGNATI_MODEL:
            body["model"] = cfg.GIGNATI_MODEL
        return f"{cfg.GIGNATI_BASE_URL}/v1/chat/completions", dict(llm._HEADERS), body
    raise ValueError(f"unknown provider {provider!r}")


def _post(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=cfg.TIMEOUT_S) as resp:
        return json.load(resp)


def _mock(payload):
    name, does = payload["company_name"], payload["what_it_does"]
    opening = (f"{name} works in {does.lower()}." if does else f"I came across {name} while looking at {payload['domain']}.")
    return ({"opening_line": opening,
             "finding_sentence": "Some tracking cookies are set on your homepage before a visitor makes any choice."},
            {"input_tokens": 0, "output_tokens": 0})


def generate(payload, provider=None):
    """
    (slots | None, {"input_tokens", "output_tokens"}). slots is None when the reply could not be parsed
    into the two strings (the caller counts that as a failed attempt). Raises ModelUnavailable when the
    provider cannot be reached or refuses (retrying 429, 5xx and timeouts first).
    """
    provider = provider or cfg.PROVIDER
    if provider == "mock":
        return _mock(payload)
    key = None
    if provider == "openai":
        key = os.environ.get(cfg.OPENAI_KEY_ENV)
        if not key:
            raise ModelUnavailable(f"{cfg.OPENAI_KEY_ENV} is not set")
    url, headers, body = build_request(provider, payload, key)
    last = None
    for attempt in range(cfg.HTTP_RETRIES + 1):
        try:
            reply = _post(url, headers, body)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429) and exc.code < 500:
                raise ModelUnavailable(f"{provider} refused the request (HTTP {exc.code})") from exc
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < cfg.HTTP_RETRIES:
            time.sleep(cfg.RETRY_WAIT_S)
    else:
        raise ModelUnavailable(f"{provider} unavailable after {cfg.HTTP_RETRIES + 1} attempts ({last})")
    usage = reply.get("usage") or {}
    tokens = {"input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": int(usage.get("completion_tokens") or 0)}
    try:
        slots = json.loads(reply["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None, tokens
    if not isinstance(slots, dict):
        return None, tokens
    return slots, tokens


def estimate_cost(input_tokens, output_tokens):
    """USD for these token counts at the configured prices (see settings_drafts: the prices are unverified defaults)."""
    return input_tokens / 1e6 * cfg.PRICE_PER_M_INPUT + output_tokens / 1e6 * cfg.PRICE_PER_M_OUTPUT


def estimate_cost_note():
    """What the cost figures rest on, so a report never presents an unchecked price as fact."""
    return {"prices_verified": cfg.PRICES_VERIFIED, "usd_per_m_input": cfg.PRICE_PER_M_INPUT,
            "usd_per_m_output": cfg.PRICE_PER_M_OUTPUT, "model": cfg.model_for(cfg.PROVIDER)}
