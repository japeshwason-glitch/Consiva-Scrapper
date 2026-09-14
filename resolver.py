"""
Domain resolution: generate broadly, verify strictly.

The previous design had it backwards -- five narrow slug variants judged on name
similarity alone. That combination produced a 69% error rate in the ambiguous
band, because name similarity is the one axis a wrong domain can pass perfectly:
marvel.com matches MARVEL KING LIMITED, gaurav.com matches GAURAV SPINNERS.

Here, generation is cheap and wide (a candidate costs one DNS lookup) and
acceptance requires agreement across INDEPENDENT axes derived from the MCA row:

    name      rapidfuzz against title/body      necessary, never sufficient
    location  registered PIN / district on page conclusive when present
    industry  NIC classification words on page  corroborating
    verdict   local LLM, with full MCA context  tiebreaker only

At least two axes must carry positive evidence. That rule rejects marvel.com
(name only), rejects vardhmantrading.in (name strong, but the page says
Ahmedabad against a Panipat registration -- the case that slipped through at
confidence 85), and accepts ronaldweboffset.com (name + PIN 121004 + industry).
"""

import json
import logging
import re
from pathlib import Path

from rapidfuzz import fuzz

import fingerprints
import llm

# Stripped from the END of a name, longest first so "LIMITED LIABILITY
# PARTNERSHIP" is removed whole rather than leaving "LIMITED LIABILITY" behind
# after "PARTNERSHIP" fails to match.
LEGAL_SUFFIXES = (
    "LIMITED LIABILITY PARTNERSHIP",   # some LLPs spell it out in full
    "PRIVATE LIMITED", "PVT LTD", "PVT. LTD.", "PVT LIMITED", "P LTD",
    "LIMITED", "LTD", "LTD.", "LLP",
    "AND COMPANY", "& COMPANY", "AND CO",
)

# Legal designations that appear ANYWHERE in a name, not just at the end.
# "(OPC)" marks a One Person Company and "AND COMPANY" often sits mid-string
# ("SAJJAN SINGH AND COMPANY ENTERPRISE") -- neither is part of the trading
# name, and both produce domains nobody owns.
INLINE_LEGAL_TERMS = (
    "OPC", "ONE PERSON COMPANY", "LIMITED LIABILITY PARTNERSHIP",
    "AND COMPANY", "& COMPANY",
)
STOPWORDS = {"THE", "INDIA", "INDIAN", "GROUP", "ENTERPRISES", "ENTERPRISE"}

# Appended by Indian SMEs surprisingly often; cheap to try, one DNS lookup each.
SME_DOMAIN_SUFFIXES = ("india", "group", "industries", "exports", "overseas", "co")

TLD_PRIORITY = (".com", ".in", ".co.in", ".net.in", ".org.in", ".biz")

# Tier 0 slugs use two or more words of the company name. Tier 1 slugs are a
# single word -- measured at 70% of all wrong domains but still 38% of correct
# ones, so they are kept and held to a higher evidence bar rather than dropped.
TIER_STRONG, TIER_WEAK = 0, 1


def clean_name(name):
    upper = (name or "").upper()
    # Drop parenthesised legal designations before punctuation is flattened,
    # so "(OPC)" disappears instead of becoming the word "OPC".
    upper = re.sub(r"\((?:OPC|ONE PERSON COMPANY)\)", " ", upper)

    out = re.sub(r"[^A-Za-z0-9 &]", " ", upper)
    out = re.sub(r"\s+", " ", out).strip()

    for term in INLINE_LEGAL_TERMS:
        out = re.sub(rf"\b{re.escape(term)}\b", " ", out)
    out = re.sub(r"\s+", " ", out).strip()

    for suffix in LEGAL_SUFFIXES:
        out = re.sub(rf"\s*{re.escape(suffix)}$", "", out).strip()
    return re.sub(r"\s+", " ", out).strip()


def _acronym(words):
    return "".join(w[0] for w in words).lower() if len(words) > 1 else ""


def slug_variants(name):
    """[(slug, tier)] -- broad by design; every candidate costs one DNS lookup."""
    words = [w for w in clean_name(name).split() if w]
    if not words:
        return []
    meaningful = [w for w in words if w not in STOPWORDS] or words

    out, seen = [], set()

    def add(slug, tier):
        slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
        if 2 < len(slug) <= 63 and slug not in seen:
            seen.add(slug)
            out.append((slug, tier))

    add("".join(meaningful), TIER_STRONG)
    add("-".join(meaningful), TIER_STRONG)
    if len(meaningful) > 2:
        add("".join(meaningful[:2]), TIER_STRONG)
        add("-".join(meaningful[:2]), TIER_STRONG)
    if len(meaningful) > 1:
        # Acronyms are dropped entirely. Across every batch run they produced
        # not one correct match -- vmp.com, sfa.com, gpc.co.in, epi.in, larp.in,
        # src.co.in -- while three- and four-letter domains are premium
        # properties that sit behind bot walls, each costing a fetch and then a
        # browser escalation for a domain that was never the company's.
        for suffix in SME_DOMAIN_SUFFIXES:
            add("".join(meaningful[:2]) + suffix, TIER_STRONG)

    # A single-word slug is only worth probing when the word is DISTINCTIVE.
    # "livguard", "eurocrit", "nxtra", "alektrify" identify a company; "shri",
    # "vijay", "diamond", "marvel", "prince" belong to whoever bought the
    # domain first, and probing them wastes a fetch to reach a certain no.
    head = meaningful[0]
    if not is_generic_word(head):
        add(head, TIER_WEAK)
        add(head + "india", TIER_WEAK)
    return out


def candidate_hosts(name):
    """[(host, tld, tier, priority)] ordered by TLD then slug strength."""
    variants = slug_variants(name)
    hosts, priority = [], 0
    for tld in TLD_PRIORITY:
        for slug, tier in variants:
            hosts.append((f"{slug}{tld}", tld, tier, priority))
            priority += 1
    return hosts


# --- evidence ---------------------------------------------------------------

PIN_RE = re.compile(r"\b\d{6}\b")

# NIC classification strings are like "Manufacturing (Machinery and Equipments)".
_INDUSTRY_NOISE = {
    "and", "of", "the", "other", "activities", "n", "e", "c", "nec", "goods",
    "products", "services", "service", "general", "related", "including",
    # generic sector words that appear on essentially every business site.
    # DRILLMAC matched "manufacturing, machinery, equipments" at 100% against a
    # completely different company in Delhi -- same sector is not same company.
    "manufacturing", "manufacture", "trading", "trade", "wholesale", "retail",
    "business", "commercial", "industry", "industries", "industrial", "supply",
    "equipment", "equipments", "solutions", "works", "company",
}


def industry_terms(classification):
    if not classification:
        return []
    words = re.findall(r"[a-z]{4,}", classification.lower())
    return [w for w in dict.fromkeys(words) if w not in _INDUSTRY_NOISE]


def name_axis(company_name, title, body):
    """0-100. Title weighted over body: a name in <title> is a far better signal."""
    target = clean_name(company_name)
    if not target:
        return 0.0
    title_score = fuzz.token_set_ratio(target, (title or "").upper())
    body_score = fuzz.token_set_ratio(target, (body or "")[:1500].upper())
    # de-spaced comparison catches "STOCK HOLDINGS" vs "Stockholdings", which
    # token matching scores at 44.9 despite being the same company
    flat_t = re.sub(r"[^A-Z0-9]", "", target)
    flat_p = re.sub(r"[^A-Z0-9]", "", (title or "").upper())
    flat_score = fuzz.ratio(flat_t, flat_p) if len(flat_t) >= 10 else 0.0
    return round(max(title_score, 0.75 * title_score + 0.25 * body_score, flat_score), 1)


def location_axis(loc, text, company_name=""):
    """
    (score 0-100, what matched). PIN is near-conclusive; a district or town name
    is good; the state alone is weak, since half of Haryana's web mentions
    Haryana.

    Tokens that appear in the company's own name are ignored. KURUKSHETRA GASES
    resolving to kurukshetra.in fired both the name and location axes on the
    same word -- two axes agreeing on one piece of evidence, which is exactly
    what the two-axis rule is supposed to prevent.
    """
    blob = (text or "").lower()
    if not blob:
        return 0.0, None
    name_words = set(clean_name(company_name).lower().split())
    if loc.get("pin") and loc["pin"] in blob:
        return 100.0, f"PIN {loc['pin']}"
    for token in sorted(loc.get("locations") or [], key=len, reverse=True):
        if set(token.split()) & name_words:
            continue  # not independent evidence -- the name already says this
        if len(token) >= 5 and re.search(rf"\b{re.escape(token)}\b", blob):
            return 70.0, f"location '{token}'"
    if loc.get("state") and re.search(rf"\b{re.escape(loc['state'])}\b", blob):
        return 30.0, f"state '{loc['state']}'"
    return 0.0, None


def industry_axis(terms, text):
    """(score 0-100, matched terms). Fraction of NIC words present on the page."""
    blob = (text or "").lower()
    if not terms or not blob:
        return 0.0, []
    hits = [t for t in terms if re.search(rf"\b{re.escape(t)}", blob)]
    return round(100.0 * len(hits) / len(terms), 1), hits


# --- decision ---------------------------------------------------------------

NAME_FLOOR = 45.0        # below this the page is not plausibly this company
LOCATION_STRONG = 70.0   # a town/district/PIN hit
INDUSTRY_STRONG = 40.0

# Single-word slugs are never accepted, at any score. ELOHI FOODS and DIAMOND
# HOMETEX both generate the correct slug (elohifoods, diamondhometex) -- those
# domains simply do not exist, and the only candidates that DO resolve are
# elohi.in and diamond.org.in, which belong to other people. Accepting the
# best available candidate turns "this company has no website" into a false
# lead. Refusing to answer is the correct answer.
# A single-word slug is only dangerous when the word is GENERIC. Banning them
# outright killed correct matches -- livguard.com, eurocrit.com, nxtra.in are
# all real. The discriminator is distinctiveness: "marvel", "diamond",
# "prince", "gaurav", "health" are dictionary words or common given names and
# collide with unrelated sites; "livguard", "eurocrit", "nxtra", "alektrify"
# are coined and collide with nothing.
_DICT_PATHS = ("/usr/share/dict/american-english", "/usr/share/dict/words")

# Common Indian given names are not in an English dictionary but behave exactly
# like generic words here -- gaurav.com, sidharth.in and sohansingh.com were all
# somebody's personal site.
COMMON_GIVEN_NAMES = {
    "gaurav", "sidharth", "siddharth", "sohan", "rahul", "amit", "ankit",
    "vikas", "vivek", "rajesh", "suresh", "ramesh", "mukul", "shyam", "tuli",
    "navneet", "shankar", "redhu", "davendra", "bhagwati", "krishna", "arjun",
    "aditya", "rohit", "manish", "sunil", "anil", "deepak", "sanjay", "vijay",
    "ajay", "rakesh", "mahesh", "naresh", "dinesh", "mohit", "nitin", "pankaj",
}

# Indian place names collide the same way -- hisar.in was a travel guide.
COMMON_PLACE_WORDS = {
    "hisar", "kurukshetra", "rohtak", "panipat", "karnal", "ambala", "sonepat",
    "gurgaon", "faridabad", "bharat", "india", "delhi", "punjab", "haryana",
}


def _load_dictionary():
    for path in _DICT_PATHS:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                return {w.strip().lower() for w in fh if w.strip().isalpha()}
        except OSError:
            continue
    return set()


_DICTIONARY = _load_dictionary()


# Words that lead several company names in the registry itself. Derived from
# the data rather than hand-listed: an English dictionary does not contain
# "shree" (43 companies), "shri" (27), "jai" (22) or "krishna" (12), yet none
# of them can identify a company. Rebuild with build_generic_words.py.
GENERIC_WORDS_PATH = Path(__file__).parent / "data" / "generic_first_words.json"
# A word is generic when this many companies share it as their first word.
#
# An absolute count, deliberately: the question is "can this word identify ONE
# company", and if 200 companies are called Royal-something then royal.com
# identifies none of them, whatever size the corpus is. A share-based rule
# looks scale-invariant but is not -- 78% of first words appear exactly once,
# so a growing corpus grows the vocabulary faster than any individual word's
# count, and the same generic word drifts below a fixed percentage.
#
# 200 is calibrated against the 1.44M-company metropolitan corpus, where it
# catches every obviously generic word (shree, shri, sai, om, jai, maa, shiv,
# bharat, krishna, royal, star, super, golden, global) while flagging none of
# the real brands we have seen resolve correctly (livguard, nxtra, pestrail,
# aisats, encalm, zealthix, kohinoor, repin). At 100 it starts eating
# "kohinoor"; at 700 it starts missing "krishna".
#
# Rebuild and re-check the calibration if the corpus changes size materially.
GENERIC_MIN_COMPANIES = 200


def _load_generic_words():
    try:
        return set(json.loads(GENERIC_WORDS_PATH.read_text()))
    except (OSError, ValueError):
        return set()


_REGISTRY_GENERIC = _load_generic_words()


def is_generic_word(word):
    """
    True if a single-word slug is too common to identify a company.

    Registry frequency only. The English dictionary was dropped: it suppressed
    "kohinoor" and "gemini", both distinctive Indian brands whose domains were
    correct, and its only remaining job -- stopping marvel.com and
    diamond.org.in -- is already done by the match-score verifier, which scored
    those 15 and 10. The dictionary saved a fetch and cost real companies.
    """
    w = (word or "").lower()
    if len(w) < 4:
        return True          # 3-letter domains are premium properties, never SMEs
    return w in _REGISTRY_GENERIC


def score_candidate(candidate, company, loc, terms):
    """Attaches every axis to the candidate and returns it."""
    text = f"{candidate['title']} {candidate['body']}"
    candidate["name_score"] = name_axis(company["company_name"],
                                        candidate["title"], candidate["body"])
    candidate["location_score"], candidate["location_hit"] = location_axis(
        loc, text, company["company_name"])
    candidate["industry_score"], candidate["industry_hits"] = industry_axis(terms, text)
    return candidate


def decide(candidate, company, tier_weak_penalty=True):
    """
    (accepted, reason). The question is NOT "which candidate scores highest" --
    that always yields an answer, which is how a company with no website ends up
    with someone else's. The question is "does this page PROVE it belongs to
    this company", and 'no' is a valid, common, correct outcome.

    Proof means the name plus at least one independent corroborating axis:
    the registered PIN/district on the page, or its industry. Name alone is
    exactly what a wrong domain passes.
    """
    name = candidate["name_score"]
    loc = candidate["location_score"]
    ind = candidate["industry_score"]
    weak = candidate.get("tier") == TIER_WEAK

    if fingerprints.is_parked(candidate.get("title", ""), candidate.get("body", ""),
                              candidate.get("domain")):
        return False, "parked or placeholder page"

    # A generic single word cannot identify a company, whatever it scores.
    if weak:
        slug = re.sub(r"\..*$", "", candidate.get("domain", "")).replace("-", "")
        if is_generic_word(slug):
            return False, (
                f"'{slug}' is a generic word or common name -- cannot identify "
                f"a company (name {name})"
            )

    if name < NAME_FLOOR:
        return False, f"name {name} below floor {NAME_FLOOR}"

    axes = []
    if loc >= LOCATION_STRONG:
        axes.append(candidate["location_hit"])
    if ind >= INDUSTRY_STRONG and len(candidate["industry_hits"]) >= 2:
        axes.append(f"industry {ind}% ({', '.join(candidate['industry_hits'][:4])})")

    if axes:
        return True, f"name {name} + " + " + ".join(axes)

    # Name matches but nothing corroborates it. Hand to the model rather than
    # guessing -- it reads the page and decides whether the business is
    # plausibly this company at all.
    return None, f"name {name}, no corroborating axis (location {loc}, industry {ind})"


def adjudicate(candidate, company, loc):
    """Local model breaks ties the axes leave open. Returns True/False/None."""
    return llm.is_company_site(
        company["company_name"], candidate["title"], candidate["body"],
        industry=company.get("industrial_classification"),
        state=company.get("state_code"),
    )


# --- the resolution flow ----------------------------------------------------

# match_score at or above this is accepted as this company's site.
#
# Raised from 60 after the metro batch: 54 of 56 accepts scored 80+, and both
# rows in the 60-79 band were broad-sector matches rather than real ones --
# AARTH CREATIVE STUDIOS accepted an accounting firm at 65 because both are
# "Business Services". Costs ~4% of accepts and removes that whole class.
# Volume is free at 1.4M records; a wrong domain is not.
_FUZZY_FALLBACKS = 0    # counted so a run can report silent degradation

ACCEPT_SCORE = 80
CORROBORATION_BONUS = 10 # registered PIN/district found on the page


def resolve(company, fetch_fn, loc=None, max_candidates=6, log=None):
    """
    (winner | None, reason, examined).

    Identity only -- nothing here looks at cookies. A company with no website
    returns None, which is the correct answer for most MCA rows and is not a
    failure of the resolver.

    fetch_fn(host) -> (final_url, title, body) or None.
    """
    name = company["company_name"]
    loc = loc if loc is not None else {"pin": None, "state": None, "locations": []}
    examined, fetched = [], 0
    transient_dns = 0

    for host, tld, tier, priority in candidate_hosts(name):
        if fetched >= max_candidates:
            break
        try:
            if not _dns_ok(host):
                continue
        except _TransientDNS:
            # Could not ask about this candidate. Say so rather than letting
            # its silence count as a point against the company.
            transient_dns += 1
            continue
        got = fetch_fn(host)
        if not got:
            continue
        final_url, title, body, status = (list(got) + [200])[:4]
        fetched += 1

        # Deterministic rejections -- no reason to spend a model call on a
        # parked page or an empty placeholder.
        if fingerprints.is_parked(title, body, host):
            examined.append({"domain": host, "tld": tld, "tier": tier, "score": 0,
                             "reason": "parked or placeholder page"})
            continue

        # A wall means we could not look. That is not evidence about identity,
        # so it is recorded for escalation rather than scored as a mismatch.
        if fingerprints.is_blocked(title, body, status):
            examined.append({"domain": host, "tld": tld, "tier": tier, "score": None,
                             "blocked": True, "status": status,
                             "reason": f"blocked by a bot wall (HTTP {status})"})
            if log:
                log(f"      {host:<38} BLOCKED  HTTP {status}")
            continue

        score, reason = match_score_for(company, title, body)
        if score is None:
            # Model unreachable. Fall back to string similarity rather than
            # silently rejecting every company during an outage -- but make it
            # LOUD. A silent degradation here accepts domains on name
            # similarity alone, which is exactly the failure that put
            # marvel.com in the lead list.
            global _FUZZY_FALLBACKS
            _FUZZY_FALLBACKS += 1
            logging.warning("verifier unavailable, falling back to fuzzy match "
                            "for %s (%d so far this run): %s",
                            company.get("company_name"), _FUZZY_FALLBACKS, reason)
            score = name_axis(name, title, body)
            reason = f"[fuzzy fallback] {reason}"

        loc_score, loc_hit = location_axis(loc, f"{title} {body}", name)
        if loc_score >= LOCATION_STRONG:
            score = min(100, score + CORROBORATION_BONUS)
            reason = f"{reason} (corroborated by {loc_hit})"

        candidate = {
            "domain": host, "tld": tld, "tier": tier, "final_url": final_url,
            "title": title[:160], "score": score, "reason": reason,
            "location_hit": loc_hit,
        }
        examined.append(candidate)
        if log:
            log(f"      {host:<38} {score:>3}  {reason[:70]}")

    if not examined:
        if transient_dns:
            # Every candidate we could have learned from went unasked. This is
            # not evidence that the company has no website.
            return None, (f"DNS_UNAVAILABLE: {transient_dns} candidate lookup(s) "
                          f"could not be completed"), []
        return None, "no candidate hostname resolved in DNS or returned a page", []

    scored = [c for c in examined if c.get("score") is not None]
    blocked = [c for c in examined if c.get("blocked")]

    if scored:
        best = max(scored, key=lambda c: c["score"])
        if best["score"] >= ACCEPT_SCORE:
            return best, best["reason"], examined
    else:
        best = None

    if blocked:
        hosts = ", ".join(c["domain"] for c in blocked[:3])
        return None, f"BLOCKED: {hosts} -- could not read the page", examined

    return None, (
        f"best candidate {best['domain']} scored {best['score']} "
        f"(< {ACCEPT_SCORE}): {best['reason']}"
    ), examined


def match_score_for(company, title, body):
    return llm.match_score(
        company["company_name"], title, body,
        industry=company.get("industrial_classification"),
    )


class DNSUnavailable(RuntimeError):
    """
    The local resolver is failing, so nothing it says is evidence about any
    domain. Raised rather than returned because a caller that treats this as
    "no website" writes a permanent verdict from a temporary outage.
    """


# Consecutive EAI_AGAIN results that mean the resolver is down rather than the
# domains being absent. This counts CANDIDATE lookups, and one company probes
# 60-70 candidates, so the threshold has to clear several companies: anything
# near 10 would abort a whole run on one company's worth of a two-second blip.
# At 200, roughly three consecutive companies have gone entirely unasked.
DNS_OUTAGE_STREAK = 200
_EAI_AGAIN_STREAK = 0


def _dns_ok(host):
    """
    True if the host resolves, False if the resolver says it does not exist.

    EAI_AGAIN is neither: it means we could not ask. That is the errno seen
    when systemd-resolved is flapping between upstream servers, and treating
    it as absence sends live companies to resolve_failed.
    """
    global _EAI_AGAIN_STREAK
    import socket
    try:
        socket.getaddrinfo(host, None)
        _EAI_AGAIN_STREAK = 0
        return True
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_AGAIN:
            _EAI_AGAIN_STREAK += 1
            if _EAI_AGAIN_STREAK >= DNS_OUTAGE_STREAK:
                raise DNSUnavailable(
                    f"{_EAI_AGAIN_STREAK} consecutive lookups failed with "
                    f"'temporary failure in name resolution' (last: {host}). "
                    f"The resolver is down, not the domains.") from exc
            raise _TransientDNS(host) from exc
        _EAI_AGAIN_STREAK = 0
        return False
    except UnicodeError:
        return False


class _TransientDNS(RuntimeError):
    """One lookup we could not complete. Skips the candidate, not the company."""
