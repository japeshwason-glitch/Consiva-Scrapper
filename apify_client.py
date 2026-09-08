"""
Minimal Apify client for the LinkedIn company actor.

Only what stage 04 needs: run an actor synchronously with a batch of company
names and return the dataset. Kept separate from the stage so the actor can be
swapped without touching pipeline logic.
"""

import json
import urllib.error
import urllib.request

import config

ACTOR = "harvestapi~linkedin-company"
BASE = "https://api.apify.com/v2"
TIMEOUT = 300


class ApifyUnavailable(RuntimeError):
    """Raised when Apify cannot be reached or refuses the run."""


def search_companies(names, timeout=TIMEOUT):
    """
    names -> list of LinkedIn company records.

    The actor accepts a batch of search terms in one run, which matters on a
    free account: one run for ten companies costs far less than ten runs.
    """
    if not config.APIFY_API_TOKEN:
        raise ApifyUnavailable("APIFY_API_TOKEN is not set")
    if not names:
        return []

    url = (f"{BASE}/acts/{ACTOR}/run-sync-get-dataset-items"
           f"?token={config.APIFY_API_TOKEN}")
    payload = json.dumps({"searches": list(names)}).encode()
    req = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ApifyUnavailable(str(exc)) from exc
    return data if isinstance(data, list) else []


def headquarters(record):
    """The HQ location dict, or the first location, or None."""
    locations = record.get("locations") or []
    for loc in locations:
        if loc.get("headquarter"):
            return loc
    return locations[0] if locations else None
