"""
Client for the self-hosted llama.cpp server (Llama 3.1 8B Instruct).

Scope, deliberately narrow -- this model is only ever asked to read text that
is already on the page and report what it says:

  * stage 03 soft fields: tagline, notable clients, founder name, headcount
  * stage 02 tie-breaking on ambiguous rapidfuzz scores

It is never used for:
  * has_cmp / CMP detection -- the cost gate, which must be deterministic and
    auditable (see fingerprints.py)
  * business-scale classification -- rule-based by design
  * any outreach copy or personalisation hook -- the CRM person writes those

Being self-hosted on the LAN, inference is free and no company data leaves the
network, but neither fact changes the rules above.
"""

import json
import re
import urllib.error
import urllib.request

import config

# The server sits behind a Cloudflare tunnel, which challenges requests that
# look automated. Browser-shaped headers keep it from serving a bot page
# instead of JSON. No Accept-Encoding: urllib will not decompress brotli.
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_ENDPOINT = "/v1/chat/completions"


class LLMUnavailable(RuntimeError):
    """Raised when the server can't be reached -- callers degrade, never crash."""


def available():
    """Cheap health check so a stage can fall back instead of failing a batch."""
    if not config.LLM_ENABLED:
        return False
    try:
        req = urllib.request.Request(f"{config.LLM_BASE_URL}/health", headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("status") == "ok"
    except Exception:
        return False


def chat(system, user, max_tokens=400, temperature=0.0):
    """One completion. temperature=0 so a rerun on the same page gives the same answer."""
    payload = json.dumps({
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        # Qwen3.x is a reasoning model: left on, it streams its chain of thought
        # into reasoning_content and returns an EMPTY content field until the
        # thinking finishes, which silently burns the token budget. We want a
        # one-word verdict, not deliberation.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()

    req = urllib.request.Request(
        f"{config.LLM_BASE_URL}{_ENDPOINT}", payload, _HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as r:
            data = json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(str(exc)) from exc

    message = data["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    if not content and message.get("reasoning_content"):
        # Thinking leaked through despite the flag -- treat as no answer rather
        # than parsing a half-finished thought.
        raise LLMUnavailable("model returned reasoning instead of an answer")
    return content


def chat_json(system, user, max_tokens=400):
    """
    chat() that insists on a JSON object. Small instruct models like to wrap
    JSON in prose or a code fence, so the object is extracted rather than
    assuming a clean response. Returns None if nothing parseable came back --
    the caller then leaves those fields NULL instead of storing a guess.
    """
    raw = chat(system, user, max_tokens=max_tokens)

    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()

    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


EXTRACT_SYSTEM = (
    "You extract facts from company website text. Reply with ONLY a JSON object "
    "and no other prose.\n"
    "Keys:\n"
    '  "tagline": the company\'s own short descriptive strapline, or null\n'
    '  "notable_clients": array of ORGANISATION names the text explicitly presents '
    "as clients or customers of this company, [] if none\n"
    '  "founder_name": full name of a founder, co-founder, MD, or CEO, or null\n'
    '  "team_headcount": integer employee count if the page states one, else null\n'
    "Rules: use ONLY facts stated in the text. Never infer, never guess, never "
    "use outside knowledge. If the text does not state something, use null or [].\n"
    "notable_clients must contain only companies or organisations. NEVER include: "
    "individual people's names, testimonial or review authors, quoted customers, "
    "staff or team members, navigation menu items, product names, or service "
    "categories. A person's name is never a client."
)


def extract_soft_fields(company_name, text):
    """
    Pulls the descriptive fields out of page text. Returns a dict with the four
    keys, values None/[] when absent. Never raises on bad model output -- an
    unparseable reply degrades to empty, same as a page that said nothing.
    """
    empty = {
        "tagline": None,
        "notable_clients": [],
        "founder_name": None,
        "team_headcount": None,
    }
    if not text or not text.strip():
        return empty

    parsed = chat_json(
        EXTRACT_SYSTEM,
        f"Company: {company_name}\n\nWebsite text:\n{text}",
        max_tokens=400,
    )
    if not parsed:
        return empty

    out = dict(empty)
    tagline = parsed.get("tagline")
    if isinstance(tagline, str) and tagline.strip():
        out["tagline"] = tagline.strip()[:300]

    clients = parsed.get("notable_clients")
    if isinstance(clients, list):
        out["notable_clients"] = [
            str(c).strip()[:100] for c in clients if str(c).strip()
        ][:15]

    founder = parsed.get("founder_name")
    if isinstance(founder, str) and founder.strip():
        out["founder_name"] = founder.strip()[:120]

    headcount = parsed.get("team_headcount")
    if isinstance(headcount, bool):
        headcount = None
    if isinstance(headcount, (int, float)) and 0 < headcount < 1_000_000:
        out["team_headcount"] = int(headcount)

    return out


MATCH_SYSTEM = (
    "You judge whether a web page is a specific company's own official website.\n"
    "Answer with exactly one word: YES or NO.\n\n"
    "Answer YES when the page is consistent with the company: it carries the "
    "company's distinctive name and nothing on the page contradicts what you "
    "were told about it.\n\n"
    "Answer NO only when the page CONTRADICTS the company:\n"
    "  - it is plainly a different business that happens to share a word\n"
    "  - it is a personal site, blog, or CV of an individual\n"
    "  - it states a different country, or an unrelated industry\n"
    "  - it sells the domain, is parked, or is an empty placeholder\n\n"
    "Missing information is NOT a contradiction. Most small company websites "
    "never state their city, their registered state, or their industry code. "
    "Absence of those details is normal and is not evidence against a match. "
    "Judge only on what the page actually says."
)


def is_company_site(company_name, title, text, industry=None, state=None):
    """
    Stage 02 tie-breaker for ambiguous rapidfuzz scores. Returns True/False, or
    None when the model is unreachable or answers with something unexpected --
    the caller then falls back to the fuzzy score alone.

    The MCA row already tells us the company's industry and home state, so that
    context is handed over too: it is what separates MIZUKY TOOLS INDIA (a
    Haryana tool maker) from "Mizuky Workshop - Games and Small Software".
    """
    context = [f"Company: {company_name}"]
    if industry:
        context.append(f"Its industry per government registry: {industry}")
    if state:
        context.append(f"Its registered state in India: {state}")

    try:
        answer = chat(
            MATCH_SYSTEM,
            "\n".join(context) + f"\n\nPage title: {title}\n"
            f"Page text: {text[:1200]}\n\nIs this that company's own website?",
            max_tokens=4,
        ).upper()
    except LLMUnavailable:
        return None
    if answer.startswith("YES"):
        return True
    if answer.startswith("NO"):
        return False
    return None


MATCH_SCORE_SYSTEM = (
    "You score how well a website's actual content matches a registered company "
    "name. Reply with ONLY a JSON object: "
    '{"score": <0-100 integer>, "reason": "<one short sentence>"}\n\n'
    "Indian company names usually state what the business DOES -- 'ELOHI FOODS' "
    "sells food, 'DIAMOND HOMETEX' makes home textiles, 'RONALD WEB OFFSET' "
    "makes printing machinery, 'VIOK CASES' makes cases. Judge the site on "
    "whether it actually does that thing, not merely on whether a word from "
    "the name appears somewhere.\n\n"
    "A shared BROAD sector is not a match. 'Manufacturing' matching "
    "'manufacturing', or 'services' matching 'services', proves nothing -- "
    "almost every company is one of those. The specific product or service "
    "must line up: a machinery maker is not a medical-device maker.\n\n"
    "Indian registered companies serve Indian customers. A site written in "
    "French, Spanish or Portuguese, priced in foreign currency, or addressed to "
    "another country's market is a different company that happens to share a "
    "name -- score it below 30 however well the name matches.\n\n"
    "Scoring guide:\n"
    "  85-100  the site clearly belongs to this company: the distinctive part "
    "of the name AND the SPECIFIC line of business both match\n"
    "  60-84   very likely this company: business matches, name is consistent\n"
    "  30-59   unclear -- a shared word but the business does not obviously fit\n"
    "  0-29    different business, a person's site, a placeholder, or a parked "
    "domain\n\n"
    "A page with almost no content, or whose title is just its own domain "
    "name, scores below 20 -- an empty site proves nothing.\n"
    "Judge only what the text says. Do not use outside knowledge of brands."
)


def match_score(company_name, title, text, industry=None):
    """
    (score 0-100, reason). How well the site's CONTENT matches what the company
    name says the business is.

    This is the primary identity test. String similarity cannot do it:
    marvel.com scores a perfect name match for MARVEL KING LIMITED while
    selling superhero films, and elohi.in matches ELOHI FOODS while being an
    empty placeholder with no food on it anywhere.

    Returns (None, reason) when the model is unreachable, so callers can fall
    back rather than silently scoring everything zero.
    """
    context = [f"Registered company name: {company_name}"]
    if industry:
        context.append(f"Registry industry classification: {industry}")

    try:
        parsed = chat_json(
            MATCH_SCORE_SYSTEM,
            "\n".join(context)
            + f"\n\nWebsite title: {title}\n\nWebsite text:\n{(text or '')[:2000]}",
            max_tokens=160,
        )
    except LLMUnavailable as exc:
        return None, f"verifier unavailable: {exc}"

    if not parsed:
        return None, "verifier returned no parseable JSON"

    score = parsed.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None, f"verifier returned a non-numeric score: {score!r}"
    reason = str(parsed.get("reason") or "").strip()[:200]
    return max(0, min(100, int(score))), reason


PROFILE_SYSTEM = (
    "You classify a company from its own website. Reply with JSON only:\n"
    '{"industry": "<one of the listed values>", "scale": "<one of the listed '
    'values>", "confidence": "high|medium|low", "reason": "<12 words>"}\n\n'
    "industry must be exactly one of: Business Services, Finance, Insurance, "
    "Trading, Manufacturing, Construction, Real Estate, Healthcare, Education, "
    "Transport, storage and Communications, Community, personal and Social "
    "Services, Other.\n\n"
    "scale must be exactly one of: Startup, MSME, SMB, Enterprise, Unknown.\n"
    "  Startup    — young, product-led, small team, often hiring engineers\n"
    "  MSME       — small local or family business, few staff, no career page\n"
    "  SMB        — established mid-size firm, multiple functions or locations\n"
    "  Enterprise — large, many offices or countries, investor or press pages\n"
    "  Unknown    — the site genuinely does not say\n\n"
    "Answer Unknown rather than guessing. A brochure site for a small firm is "
    "MSME, not Enterprise, however polished it looks."
)

_INDUSTRIES = {
    "business services", "finance", "insurance", "trading", "manufacturing",
    "construction", "real estate", "healthcare", "education",
    "transport, storage and communications",
    "community, personal and social services", "other",
}
_SCALES = {"startup", "msme", "smb", "enterprise", "unknown"}


def profile_company(name, title, text, signals=None):
    """
    (industry, scale, confidence, reason) inferred from a company's own site.

    For inbound leads there is no registry row to read capital or entity type
    from, so the site is the only evidence. Returns (None, "Unknown", ...) when
    the model is unreachable, so a caller writes nothing rather than a guess.
    """
    context = [f"Company: {name}"]
    for key, label in (("open_roles_count", "open roles advertised"),
                       ("notable_open_roles", "roles"),
                       ("tech_ecommerce_platform", "ecommerce platform"),
                       ("tech_cms", "CMS"),
                       ("notable_clients", "named clients"),
                       ("footer_address", "address")):
        value = (signals or {}).get(key)
        if value:
            context.append(f"{label}: {value}")

    try:
        parsed = chat_json(
            PROFILE_SYSTEM,
            "\n".join(context) + f"\n\nWebsite title: {title}\n\n"
            f"Website text:\n{(text or '')[:2500]}",
            max_tokens=200,
        )
    except LLMUnavailable as exc:
        return None, "Unknown", "low", f"profiler unavailable: {exc}"
    if not parsed:
        return None, "Unknown", "low", "profiler returned no parseable JSON"

    industry = str(parsed.get("industry") or "").strip()
    scale = str(parsed.get("scale") or "").strip()
    if industry.lower() not in _INDUSTRIES:
        industry = None
    if scale.lower() not in _SCALES:
        scale = "Unknown"
    else:
        scale = {"msme": "MSME", "smb": "SMB"}.get(scale.lower(), scale.title())
    return (industry, scale, str(parsed.get("confidence") or "low"),
            str(parsed.get("reason") or "")[:200])
