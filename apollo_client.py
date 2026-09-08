"""
Apollo client, scoped to what the free tier allows.

people SEARCH is free and returns first name, an obfuscated surname, job title
and a has_email flag. That is enough to identify the right PERSONA at a company.

people/match with reveal_personal_emails is a PAID credit per call and is
gated behind config.APOLLO_ALLOW_EMAIL_REVEAL, which is off. The function that
would call it refuses rather than spending credits this account does not have.
"""

import json
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


def _post(url, payload):
    if not config.APOLLO_API_KEY:
        raise ApolloUnavailable("APOLLO_API_KEY is not set")
    req = urllib.request.Request(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "Cache-Control": "no-cache",
         "x-api-key": config.APOLLO_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
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
