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
_left = {}


def requests_left():
    """Last-seen remaining allowance per window, or {} before the first call."""
    return dict(_left)


def _note_limits(headers):
    for h in _LIMIT_HEADERS:
        v = headers.get(h)
        if v is not None:
            try:
                _left[h] = int(v)
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
    day = _left.get("x-24-hour-requests-left")
    minute = _left.get("x-minute-requests-left")
    hour = _left.get("x-hourly-requests-left")

    if day is not None and day <= 0:
        raise ApolloBudgetExhausted(
            "24-hour Apollo allowance is spent; remaining companies left "
            "untouched for the next run")
    if minute is not None and minute <= 1:
        logging.info("apollo: minute allowance spent, waiting 60s")
        time.sleep(60)
        _left["x-minute-requests-left"] = 50
    elif hour is not None and hour <= 1:
        logging.warning("apollo: hourly allowance spent, waiting 5 min")
        time.sleep(300)
        _left["x-hourly-requests-left"] = 50


def _post(url, payload):
    if not config.APOLLO_API_KEY:
        raise ApolloUnavailable("APOLLO_API_KEY is not set")
    req = urllib.request.Request(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "Cache-Control": "no-cache",
         "x-api-key": config.APOLLO_API_KEY})
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
            if _left.get("x-24-hour-requests-left", 1) <= 0:
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
