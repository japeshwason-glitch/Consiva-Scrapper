"""
Apollo client, scoped to what the free tier allows.

people SEARCH is free and returns first name, an obfuscated surname, job title
and a has_email flag. That is enough to identify the right PERSONA at a company.

people/match with reveal_personal_emails is a PAID credit per call and is
gated behind config.APOLLO_ALLOW_EMAIL_REVEAL, which is off. The function that
would call it refuses rather than spending credits this account does not have.
"""

import json
import logging
import time
import urllib.error
import urllib.request

import config

BASE = "https://api.apollo.io/api/v1"
# /mixed_people/search is deprecated for API callers and returns an error;
# api_search is the supported endpoint. Confirmed live, and CLAUDE.md says the
# same.
SEARCH_ENDPOINT = f"{BASE}/mixed_people/api_search"
MATCH_ENDPOINT = f"{BASE}/people/match"
TIMEOUT = 45


class ApolloUnavailable(RuntimeError):
    pass


class ApolloPaidFeature(RuntimeError):
    """Raised rather than silently spending a credit the account lacks."""


class ApolloBudgetExhausted(RuntimeError):
    """
    The 24-hour request allowance is gone. Distinct from ApolloUnavailable so
    callers stop the run instead of marking every remaining company failed --
    "we could not ask" is not "this company has no contact".
    """


# Apollo publishes its own limits on every response. The free plan is
# 50/minute, 200/hour, 600/24h, measured live. Rather than hard-coding those,
# read them back and pace against whatever the account actually has.
_LIMIT_HEADERS = ("x-minute-requests-left", "x-hourly-requests-left",
                  "x-24-hour-requests-left")

# Allowances are per API key, so each key in the pool is tracked separately and
# the pool is only exhausted once every key is. With one key configured this is
# exactly the old single-key behaviour.
_left = {}          # key -> {header: remaining}
_key_index = 0
_spent = set()      # keys whose 24-hour allowance is gone


def _keys():
    return config.APOLLO_API_KEYS or ([config.APOLLO_API_KEY]
                                      if config.APOLLO_API_KEY else [])


def current_key():
    keys = _keys()
    return keys[_key_index % len(keys)] if keys else None


def _rotate():
    """
    Move to the next key with allowance left. True if one was found.

    Rotation is what makes a spent key a pause rather than the end of the day:
    the run continues on the next key and only stops when all are spent.
    """
    global _key_index
    keys = _keys()
    for step in range(1, len(keys) + 1):
        candidate = keys[(_key_index + step) % len(keys)]
        if candidate not in _spent:
            _key_index = (_key_index + step) % len(keys)
            logging.warning("apollo: key %d/%d spent, switching to key %d",
                            (_key_index - step) % len(keys) + 1, len(keys),
                            _key_index + 1)
            return True
    return False


def requests_left():
    """Last-seen remaining allowance for the key in use, or {} before use."""
    return dict(_left.get(current_key(), {}))


def pool_status():
    """(keys configured, keys with allowance left) -- for logging a run."""
    keys = _keys()
    return len(keys), len([k for k in keys if k not in _spent])


def _note_limits(headers):
    slot = _left.setdefault(current_key(), {})
    for h in _LIMIT_HEADERS:
        v = headers.get(h)
        if v is not None:
            try:
                slot[h] = int(v)
            except ValueError:
                pass


def _pace():
    """
    Sleep before a call that would breach a window. The minute window is the
    one that bites -- 50 calls go out in a few seconds otherwise, and every
    call after that is a 429.
    """
    # Before the first call nothing is known, and an unknown window must not
    # be treated as an exhausted one -- that slept 60s before call number one.
    seen = _left.get(current_key(), {})
    day = seen.get("x-24-hour-requests-left")
    minute = seen.get("x-minute-requests-left")
    hour = seen.get("x-hourly-requests-left")

    if day is not None and day <= 0:
        _spent.add(current_key())
        if _rotate():
            return          # the next key has its own allowance; carry on
        raise ApolloBudgetExhausted(
            f"every configured Apollo key ({len(_keys())}) has spent its "
            f"24-hour allowance; remaining companies left for the next run")
    if minute is not None and minute <= 1:
        logging.info("apollo: minute allowance spent, waiting 60s")
        time.sleep(60)
        _left["x-minute-requests-left"] = 50
    elif hour is not None and hour <= 1:
        logging.warning("apollo: hourly allowance spent, waiting 5 min")
        time.sleep(300)
        _left["x-hourly-requests-left"] = 50


def _post(url, payload):
    if not current_key():
        raise ApolloUnavailable("no Apollo API key configured "
                                "(APOLLO_API_KEY or APOLLO_API_KEYS)")
    req = urllib.request.Request(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "Cache-Control": "no-cache",
         "x-api-key": current_key()})
    _pace()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            _note_limits({k.lower(): v for k, v in r.headers.items()})
            return json.load(r)
    except urllib.error.HTTPError as exc:
        _note_limits({k.lower(): v for k, v in exc.headers.items()})
        body = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 429:
            # Paced wrong, or another process shares the key. Wait out the
            # minute window once and retry rather than burning the company.
            if _left.get(current_key(), {}).get("x-24-hour-requests-left", 1) <= 0:
                _spent.add(current_key())
                if not _rotate():
                    raise ApolloBudgetExhausted(body) from exc
            logging.warning("apollo: 429, waiting 60s and retrying once")
            time.sleep(60)
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    _note_limits({k.lower(): v for k, v in r.headers.items()})
                    return json.load(r)
            except urllib.error.HTTPError as exc2:
                body2 = exc2.read().decode("utf-8", "replace")[:300]
                raise ApolloUnavailable(f"HTTP {exc2.code}: {body2}") from exc2
        raise ApolloUnavailable(f"HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ApolloUnavailable(str(exc)) from exc


def search_people(domain, titles=None, per_page=25):
    """People at this domain. Free on every plan."""
    payload = {"q_organization_domains": domain, "page": 1, "per_page": per_page}
    if titles:
        payload["person_titles"] = list(titles)
    data = _post(SEARCH_ENDPOINT, payload)
    return data.get("people") or [], data.get("total_entries", 0)


def reveal_email(person_id):
    """
    Would consume one paid credit. Deliberately refuses while the account is on
    the free tier -- flip APOLLO_ALLOW_EMAIL_REVEAL in .env to enable.
    """
    if not config.APOLLO_ALLOW_EMAIL_REVEAL:
        raise ApolloPaidFeature(
            "email reveal is disabled (APOLLO_ALLOW_EMAIL_REVEAL=0); "
            "free plan has no credits for people/match")
    return _post(MATCH_ENDPOINT, {"id": person_id, "reveal_personal_emails": True})
