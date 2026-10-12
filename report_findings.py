"""
Findings engine for the one-page DPDP report: a report-mode scan in, the gaps,
review points, summary and score out. Pure and deterministic -- no I/O beyond
reading the catalogue, no model, no randomness -- so the same scan always
produces the same report.

    evaluate(scan) -> {"findings", "gaps", "reviews", "summary", "score",
                       "band", "band_label", "score_line", "review_label",
                       "gap_count", "review_count"}

`scan` is a report_scans row: has_cmp, cmp_name, cmp_evidence,
has_cookie_banner, has_privacy_policy, cookies (records with category and
vendor), third_party_tracking_domains, has_data_form, form_field_types,
form_consent_checkbox, form_privacy_link, form_third_party_hosts, final_domain.

Every customer-facing word comes from report_catalogue.yaml; this module only
fills placeholders. The score is fingerprints.consent_score() itself -- the
formula the CRM's GapScore is computed with -- so the PDF and the CRM always
show the same number for the same scan.

Tracking cookies are advertising / session_replay / analytics only: necessary
and unclassified cookies never count toward a finding or the score.
"""
from pathlib import Path

import yaml

import fingerprints as fp

CATALOGUE_PATH = Path(__file__).resolve().parent / "report_catalogue.yaml"
CATALOGUE = yaml.safe_load(CATALOGUE_PATH.read_text())
CATALOGUE_VERSION = str(CATALOGUE["version"])

MAX_GAPS = 4
MAX_REVIEWS = 1
MAX_VENDOR_GROUPS = 4
MAX_NAMES_PER_GROUP = 3
GAP_ORDER = ("G5", "G1", "G2", "G3", "G4")
REVIEW_ORDER = ("A2",)
OTHER_VENDORS = object()   # marks the overflow group until it is given its display name

# Detail levels for fitting one page. The template is fixed, so a site with
# many trackers is made to fit by showing less detail, never by clipping:
# report_build renders level 0 and steps up only while the page overflows.
# (vendor groups, names per group, third-party domains listed)
COMPACT_LEVELS = ((4, 3, None), (4, 2, 3), (3, 2, 3), (2, 1, 2))

# The order form fields are named in, most identifying first.
FIELD_ORDER = ("name", "email", "phone", "address", "company", "message")


def word(key, n):
    """The catalogue's singular or plural for a count."""
    one, many = CATALOGUE["words"][key]
    return one if n == 1 else many


def human_list(items):
    """['A', 'B', 'C'] -> 'A, B and C', with the catalogue's separators."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return CATALOGUE["list_sep"].join(items[:-1]) + CATALOGUE["list_and"] + items[-1]


def cmp_display(name):
    """Customer-facing CMP name; internal labels such as '(WP)' never print."""
    return CATALOGUE["cmp_names"][name] if name else name


def vendor_display(vendor):
    return CATALOGUE["vendor_names"][vendor or "Other"]


def tracking_cookies(scan):
    return [c for c in scan.get("cookies") or [] if c.get("category") in fp.TRACKING_CATEGORIES]


def youtube_only(tracking):
    """True when there are tracking cookies and every one is YouTube's (an embed)."""
    return bool(tracking) and all(c.get("vendor") == "YouTube" for c in tracking)


def cookie_groups(tracking, max_groups=MAX_VENDOR_GROUPS, max_names=MAX_NAMES_PER_GROUP):
    """
    Tracking cookies grouped by vendor, busiest vendor first. At most
    max_groups groups of at most max_names names; past that the last group
    becomes the catalogue's "Other vendors", and the names not shown are
    counted in `more` and spelled out in `more_text`.
    """
    names_by_vendor = {}
    for c in tracking:
        names = names_by_vendor.setdefault(c.get("vendor") or "Other", [])
        if c["name"] not in names:
            names.append(c["name"])
    vendors = sorted(names_by_vendor, key=lambda v: (-len(names_by_vendor[v]), v))
    if len(vendors) > max_groups:
        keep = vendors[:max_groups - 1]
        rest = [n for v in vendors[max_groups - 1:] for n in names_by_vendor[v]]
        groups = [(v, names_by_vendor[v]) for v in keep] + [(OTHER_VENDORS, rest)]
    else:
        groups = [(v, names_by_vendor[v]) for v in vendors]

    out = []
    for vendor, names in groups:
        more = max(0, len(names) - max_names)
        other = vendor is OTHER_VENDORS
        out.append({
            "vendor": CATALOGUE["other_vendors"] if other else vendor_display(vendor),
            "in_sentence": (CATALOGUE["other_vendors_in_sentence"] if other
                            else vendor_display(vendor)),
            "names": names[:max_names],
            "more": more,
            "more_text": CATALOGUE["more_text"].format(n=more) if more else "",
        })
    return out


def form_gap(scan):
    """fingerprints.form_gap()'s output, as far as consent_score() reads it."""
    return {"has_data_form": bool(scan.get("has_data_form")),
            "consent_checkbox": scan.get("form_consent_checkbox"),
            "privacy_link": scan.get("form_privacy_link"),
            "external_hosts": scan.get("form_third_party_hosts") or []}


def score(scan):
    """
    The CRM GapScore for this scan: fingerprints.consent_score() over the
    scan's own cookie records, form findings and CMP evidence. 0 = most
    severe, 100 = no measured gap (or a real CMP with nothing firing).
    """
    analysis = fp.summarise_records(scan.get("cookies") or [])
    return fp.consent_score(bool(scan.get("has_cmp")), analysis, scan.get("has_cookie_banner"),
                            scan.get("has_privacy_policy"), form_gap(scan),
                            scan.get("cmp_evidence"))


def _fields(scan):
    types = scan.get("form_field_types") or []
    ordered = [f for f in FIELD_ORDER if f in types] + sorted(set(types) - set(FIELD_ORDER))
    return human_list(CATALOGUE["form_fields"].get(f, f) for f in ordered)


def _domain_text(domains, limit):
    sep = CATALOGUE["list_sep"]
    if limit is None or len(domains) <= limit:
        return sep.join(domains)
    return (sep.join(domains[:limit]) + CATALOGUE["cookie_list"]["more_sep"]
            + CATALOGUE["more_text"].format(n=len(domains) - limit))


def _row(code, **fmt):
    entry = CATALOGUE["findings"][code]
    return {"code": code, "kind": entry["kind"], "title": entry["title"],
            "observed": entry["observed"].format(**fmt), "sections": entry["sections"],
            "requirement": entry["requirement"]}


def evaluate(scan, compact=0):
    """
    compact picks a COMPACT_LEVELS entry: it changes only how much detail the
    report rows show. Codes, score, summary and evidence are the same at every
    level, and evidence always carries every tracking cookie and domain.
    """
    max_groups, max_names, max_domains = COMPACT_LEVELS[compact]
    tracking = tracking_cookies(scan)
    n = len(tracking)
    groups = cookie_groups(tracking, max_groups, max_names)
    vendors = [g["in_sentence"] for g in cookie_groups(tracking)]
    domains = list(scan.get("third_party_tracking_domains") or [])
    k = len(domains)
    all_cookies = [{"name": c["name"], "vendor": c.get("vendor"), "domain": c.get("domain")}
                   for c in tracking]
    real_cmp = bool(scan.get("has_cmp")) and scan.get("cmp_evidence") in fp.REAL_CMP_EVIDENCE
    banner, policy = scan.get("has_cookie_banner"), scan.get("has_privacy_policy")
    form = bool(scan.get("has_data_form"))
    cmp_name = cmp_display(scan.get("cmp_name"))

    fired = {}
    if real_cmp and n:
        fired["G5"] = (_row("G5", cmp_name=cmp_name, n=n, cookies_word=word("cookie", n)),
                       {"cmp_name": scan.get("cmp_name"), "cmp_evidence": scan.get("cmp_evidence"),
                        "n": n, "cookies": all_cookies})
    if not real_cmp and n:
        fired["G1"] = (_row("G1", n=n, cookies_word=word("cookie", n)),
                       {"n": n, "cookies": all_cookies,
                        "cmp_name": scan.get("cmp_name"), "cmp_evidence": scan.get("cmp_evidence")})
    if "G1" in fired and domains:
        fired["G2"] = (_row("G2", k=k, domains_word=word("domain", k),
                            domains=_domain_text(domains, max_domains)),
                       {"domains": domains})
    if "G1" in fired and not banner:
        fired["G3"] = (_row("G3"), {"has_cookie_banner": False})
    if not policy and ("G1" in fired or "G5" in fired or form):
        fired["G4"] = (_row("G4"), {"has_privacy_policy": False, "has_data_form": form})
    if form and not scan.get("form_consent_checkbox"):
        fired["A2"] = (_row("A2", fields=_fields(scan)),
                       {"form_field_types": scan.get("form_field_types") or []})

    gap_codes = [c for c in GAP_ORDER if c in fired][:MAX_GAPS]
    review_codes = [c for c in REVIEW_ORDER if c in fired][:MAX_REVIEWS]

    def for_template(code):
        return {**fired[code][0], "cookie_groups": groups if code in ("G1", "G5") else None}

    gaps = [for_template(c) for c in gap_codes]
    reviews = [for_template(c) for c in review_codes]
    findings = [{"code": c, "kind": fired[c][0]["kind"], "sections": fired[c][0]["sections"],
                 "evidence": fired[c][1]} for c in gap_codes + review_codes]

    s = score(scan)
    band = "priority" if fp.is_severe_score(s) else "attention"
    return {
        "findings": findings,
        "gaps": gaps,
        "reviews": reviews,
        "gap_count": len(gaps),
        "review_count": len(reviews),
        "summary": summary(scan, gap_codes, n, vendors, k, cmp_name),
        "score": s,
        "band": band,
        "band_label": CATALOGUE["bands"][band],
        "score_line": score_line(len(gaps), len(reviews)),
        "review_label": CATALOGUE["review_label"],
        "cookie_list": CATALOGUE["cookie_list"],
        "note": CATALOGUE["youtube_only_note"] if youtube_only(tracking) else "",
    }


def score_line(gap_count, review_count):
    lines = CATALOGUE["score_line"]
    line = lines["gaps"].format(n=gap_count, gaps_word=word("gap", gap_count))
    if review_count:
        line += lines["reviews"].format(m=review_count, points_word=word("point", review_count))
    return line


def summary(scan, gap_codes, n, vendors, k, cmp_name):
    """The gap summary: the catalogue's sentences, filled from the scan."""
    if not gap_codes:
        return ""
    t = CATALOGUE["summary"]
    count = len(gap_codes)
    parts = [t["opening"].format(N=count, gaps_word=word("gap", count),
                                 domain=scan.get("final_domain"))]
    fill = {"n": n, "cookies_word": word("cookie", n), "vendors": human_list(vendors),
            "are_word": word("is", n), "cmp_name": cmp_name}
    if "G5" in gap_codes:
        parts.append(t["g5"].format(**fill))
    elif "G1" in gap_codes:
        text = t["g1"].format(**fill)
        if "G2" in gap_codes:
            text += t["g2_suffix"].format(k=k, domains_word=word("domain", k))
        parts.append(text + t["g1_end"])
    g3, g4 = "G3" in gap_codes, "G4" in gap_codes
    if g3 and g4:
        parts.append(t["g3_and_g4"])
    elif g3:
        parts.append(t["g3_only"])
    elif g4:
        parts.append(t["g4_only"])
    return " ".join(parts)


def helps():
    return [dict(h) for h in CATALOGUE["helps"]]
