"""
Registered-office location, parsed from the MCA address string.

Addresses look like:
    PLOT NO.190,SECTOR-7,IMT MANESAR,GURGAON,Gurgaon,Haryana,122050-India
    Khasra No- 67/19-20,,Rohad Bye-Pass,,Bahadurgarh,Jhajjar,Haryana,124507-India

The field order is not reliable -- one real row parses its district as
"HARYANA" and another as a street name. So rather than trusting positions, the
PIN anchors the parse and everything between the street lines and the PIN is
kept as a SET of candidate location tokens. Matching any one of them against a
web page is the signal; which slot it came from does not matter.
"""

import re

PIN_RE = re.compile(r"\b(\d{6})\b")

# Words that appear in address tails but carry no locational signal.
_NOISE = {
    "india", "indian", "near", "opp", "opposite", "road", "street", "plot",
    "sector", "phase", "block", "floor", "ward", "village", "distt", "dist",
    "district", "post", "office", "po", "tehsil", "industrial", "area", "estate",
    "colony", "nagar", "extn", "extension", "khasra", "no", "gali", "market",
    # generic words that survive the tail but prove nothing when matched on a
    # page -- "center", "first", "urban" appear on half the web
    "urban", "rural", "first", "second", "third", "ground", "center", "centre",
    "growth", "main", "cross", "park", "tower", "house", "complex", "building",
    "scheme", "unit", "shop", "premises", "land", "part", "side", "back",
    "front", "north", "south", "east", "west", "upper", "lower", "chowk",
    "bypass", "pass", "line", "lane", "marg", "path", "site", "plaza", "point",
}

INDIAN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil nadu",
    "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal",
    "delhi", "new delhi", "jammu and kashmir", "ladakh", "puducherry",
    "chandigarh", "andaman and nicobar islands", "dadra and nagar haveli",
    "daman and diu", "lakshadweep",
}


def parse(address):
    """
    Returns {pin, state, locations}. `locations` is a set of lowercase tokens
    (town / district / taluk names) worth looking for on a candidate website.
    """
    if not address:
        return {"pin": None, "state": None, "locations": []}

    pin_match = PIN_RE.search(address)
    pin = pin_match.group(1) if pin_match else None
    # 000000 shows up as a placeholder in real rows -- not a location.
    if pin == "000000":
        pin = None

    parts = [p.strip() for p in address.split(",") if p.strip()]

    state = None
    for part in reversed(parts):
        candidate = PIN_RE.sub("", part).replace("-India", "").strip().lower()
        if candidate in INDIAN_STATES:
            state = candidate
            break

    # Everything in the address tail that looks like a place name. The tail is
    # where town/district sit; street lines are at the front.
    locations = set()
    for part in parts[-5:]:
        token = PIN_RE.sub("", part).replace("-India", "").strip()
        token = re.sub(r"[^A-Za-z ]", " ", token)
        token = re.sub(r"\s+", " ", token).strip().lower()
        if not token or token in INDIAN_STATES or token == "india":
            continue
        words = [w for w in token.split() if w not in _NOISE and len(w) > 3]
        if not words:
            continue
        # keep the whole phrase and its meaningful words
        if len(token) > 3:
            locations.add(token)
        locations.update(words)

    return {"pin": pin, "state": state, "locations": sorted(locations)}
