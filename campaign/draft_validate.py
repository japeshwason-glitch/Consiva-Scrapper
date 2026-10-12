"""
Checks on the two sentences a model writes, run in code after generation. A slot that fails any rule is
regenerated once and then replaced by the plain fallback (draft_templates.fallback_slots).

Rules, in the order they are reported:
  empty, length, markup, email, url, sentences, number, name, banned_word, legal_claim

validate_slot() returns the list of failed rule names for one slot; validate() returns
{slot: [rules]} for the slots that failed, so an empty dict means the pair is clean.
"""
import re

import report_findings as rf

from . import draft_findings
from . import settings_drafts as cfg

SLOTS = ("opening_line", "finding_sentence")
_DOMAIN = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", re.I)
_URL = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.[a-z]{2,}/\S*", re.I)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")


def _known_names():
    names = set(cfg.EXTRA_NAMES)
    for key, display in rf.CATALOGUE["vendor_names"].items():
        names.add(key)
        names.add(re.sub(r"\s*\(.*\)", "", display))
    names.update(rf.CATALOGUE["cmp_names"].values())
    names.discard("Other")
    return sorted((n for n in names if n), key=len, reverse=True)


def _numbers_in(text):
    found = set(re.findall(r"\d+", text))
    for word in re.findall(r"[A-Za-z]+", text):
        n = cfg.NUMBER_WORDS.get(word.lower())
        if n is not None:
            found.add(str(n))
    return found


def _bad_names(text, allowed):
    """Names in `text` (known vendors and products, and anything shaped like a domain) the findings lack."""
    bad, rest = [], text
    for name in _known_names():
        flags = 0 if name in cfg.COMMON_WORD_NAMES else re.I
        pattern = re.compile(rf"(?<![\w.]){re.escape(name)}(?!\w)(?!\.\w)", flags)   # not inside a domain
        if pattern.search(rest):
            if name.lower() not in allowed:
                bad.append(name)
            rest = pattern.sub(" ", rest)           # consumed: "Google Ads" must not also count as "Google"
    for token in _DOMAIN.findall(rest):
        if token.lower() not in allowed:
            bad.append(token)
    return bad


def validate_slot(slot, text, findings):
    if not isinstance(text, str) or not text.strip():
        return ["empty"]
    text = text.strip()
    fails = []
    low, high = cfg.LENGTH_LIMITS[slot]
    if not low <= len(text) <= high:
        fails.append("length")
    if re.search(r"[<>&`]", text):
        fails.append("markup")
    if _EMAIL.search(text):
        fails.append("email")
    if _URL.search(text):
        fails.append("url")
    if ("\n" in text or not text.endswith(".") or re.search(r"[!?]", text)
            or re.search(r"\.\s+[A-Z]", text)):
        fails.append("sentences")
    if not _numbers_in(text) <= draft_findings.allowed_numbers(findings):
        fails.append("number")
    if _bad_names(text, draft_findings.allowed_names(findings)):
        fails.append("name")
    if any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", text, re.I) for w in cfg.BANNED_WORDS):
        fails.append("banned_word")
    if any(re.search(p, text, re.I) for p in cfg.LEGAL_PATTERNS):
        fails.append("legal_claim")
    return fails


def validate(slots, findings):
    """{slot: [failed rules]} for the slots that failed; {} when both are clean. Extra keys are ignored."""
    slots = slots or {}
    out = {}
    for slot in SLOTS:
        fails = validate_slot(slot, slots.get(slot), findings)
        if fails:
            out[slot] = fails
    return out
