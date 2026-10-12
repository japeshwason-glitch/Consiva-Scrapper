"""
The structured findings a draft is built from: the same facts as the report, taken from the stored scan
(report_scans row) and report_findings.evaluate(). They go to the model as data and they are the only
source the validators accept numbers and names from.

Nothing about the contact is in here.
"""
import re

import report_findings as rf

from . import settings_drafts as cfg


def _scan_date(when):
    return f"{when.day} {when:%B %Y}" if when else None


def build_findings(scan, company):
    """
    scan: a report_scans row. company: {company_name, domain, industry, band, score}.
    Returns a JSON-serialisable dict.
    """
    result = rf.evaluate(scan)
    tracking = rf.tracking_cookies(scan)
    vendors = [g["vendor"] for g in rf.cookie_groups(tracking, max_groups=99, max_names=99)
               if g["vendor"] != rf.CATALOGUE["other_vendors"]]
    domains = list(scan.get("third_party_tracking_domains") or [])
    form = bool(scan.get("has_data_form"))
    top = result["findings"][0]["code"] if result["findings"] else None
    real_cmp = bool(scan.get("has_cmp")) and scan.get("cmp_evidence") in ("script_src", "dom")
    return {
        "company_name": company["company_name"],
        "domain": company["domain"],
        "what_it_does": (company.get("industry") or None),
        "band": company["band"],
        "score": company["score"],
        "gap_count": result["gap_count"],
        "scan_date": _scan_date(scan.get("scanned_at")),
        "top_finding": {"code": top, "title": rf.CATALOGUE["findings"][top]["title"] if top else None},
        "finding_codes": [f["code"] for f in result["findings"]],
        "tracking_cookie_count": len(tracking),
        "third_party_count": len(domains),
        "tracker_vendors": vendors,
        "third_party_domains": domains,
        "cookie_banner": bool(scan.get("has_cookie_banner")),
        "privacy_policy": bool(scan.get("has_privacy_policy")),
        "cmp": rf.cmp_display(scan.get("cmp_name")) if real_cmp else None,
        "form": {"collects_data": form,
                 "fields": list(scan.get("form_field_types") or []) if form else [],
                 "consent_checkbox": scan.get("form_consent_checkbox") if form else None,
                 "privacy_link": scan.get("form_privacy_link") if form else None,
                 "third_party_hosts": list(scan.get("form_third_party_hosts") or []) if form else []},
    }


def _walk(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    else:
        yield value


def allowed_numbers(findings):
    """Every number that appears in the findings (as a number or inside a string), as digit strings."""
    out = set()
    for v in _walk(findings):
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int):
            out.add(str(v))
        elif isinstance(v, str):
            out.update(re.findall(r"\d+", v))
    return out


def allowed_names(findings):
    """Lower-case vendor, product, domain and company names the findings contain."""
    f = findings
    names = {f["company_name"], f["domain"], f.get("cmp")}
    names.update(f.get("tracker_vendors") or [])
    names.update(f.get("third_party_domains") or [])
    names.update((f.get("form") or {}).get("third_party_hosts") or [])
    return {n.lower() for n in names if n}
