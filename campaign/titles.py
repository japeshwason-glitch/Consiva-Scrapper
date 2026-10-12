"""
Job titles: normalisation and the rule classifier. Pure; no I/O.

    normalize_title("Sr. Manager - IT")        -> "sr manager it"
    classify_by_rules("Head of IT")            -> {"function": "tech", "seniority": 5, "relevance": 5, "rule": ...}
    classify_by_rules("Strategy Consultant")   -> None   (leave it to the model)

The patterns, scales and weights are all in settings_titles.py.
"""
import re

from . import settings_titles as st

_COMPILED_FUNCTIONS = [(f, re.compile(p), label) for f, p, label in st.TITLE_RULES]
_COMPILED_SENIORITY = [(level, re.compile(p)) for level, p in st.SENIORITY_RULES]
_ACRONYM_DOTS = re.compile(r"\b(?:[a-z]\.)+[a-z]?\b\.?")


def normalize_title(title):
    """
    The cache key and the text the rules read: lowercase, "&" as "and", dots inside an acronym
    removed ("I.T." -> "it"), every other punctuation mark a space, runs of space collapsed.
    """
    if not title:
        return ""
    s = title.lower().replace("&", " and ")
    s = _ACRONYM_DOTS.sub(lambda m: m.group(0).replace(".", ""), s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def seniority_of(normalized):
    for level, pattern in _COMPILED_SENIORITY:
        if pattern.search(normalized):
            return level
    return None


def relevance_for(function, seniority):
    base = st.FUNCTION_BASE_RELEVANCE[function]
    return max(0, min(5, base - st.SENIORITY_PENALTY[seniority]))


def classify_by_rules(title):
    """The rule classification of a title, or None when no function pattern matches it."""
    norm = normalize_title(title)
    if not norm:
        return None
    for function, pattern, label in _COMPILED_FUNCTIONS:
        if pattern.search(norm):
            seniority = seniority_of(norm) or st.DEFAULT_SENIORITY
            return {"function": function, "seniority": seniority,
                    "relevance": relevance_for(function, seniority), "rule": label}
    return None
