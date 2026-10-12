"""
Scan fixtures for the campaign tests: result dicts shaped exactly like
stages/03_scrape_playwright.scrape_one(report_mode=True) returns, built with the
scanner's own scoring and summary functions, so the score, the six CRM values and the
findings are the real ones for a given set of cookies.

Calibrated against the real functions (2026-10-12):
    GA_ONLY_BANNER      score 90, 1 gap  (G1)            -> band D, the top of the gate
    GA_ONE_COOKIE       score 96, 1 gap  (G1)            -> score_too_high
    CLEAN               score 100, 0 gaps                -> no_gaps
    FORM_ONLY           score 75, 0 gaps (A2 advisory)   -> no_gaps, although the score is <= 90
    GA_NO_BANNER        score 64, 2 gaps (G1, G3)        -> band C
    ADS_NO_BANNER       score 46, 3 gaps                 -> band B
    ADS_NO_POLICY       score 30, 4 gaps                 -> band B
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fingerprints as fp
import report_build as rb

s3 = rb.s3
DOMAIN = "acme.example.com"


def cookie(name, domain=DOMAIN, party="first", category="analytics", life=399, vendor="Google Analytics"):
    return {"name": name, "domain": domain, "party": party, "category": category,
            "lifetime_days": life, "vendor": vendor}


GA = [cookie("_ga"), cookie("_gid"), cookie("_gat")]
ADS = [cookie("_fbp", "facebook.com", "third", "advertising", 90, "Meta Pixel"),
       cookie("IDE", "doubleclick.net", "third", "advertising", 390, "Google Ads")]
FORM = {"has_data_form": True, "field_categories": ["email", "name"], "forms_collecting": 1,
        "consent_checkbox": False, "privacy_link": False, "external_hosts": []}


def scan_data(cookies=(), cmp=None, evidence=None, banner=False, policy=True, form=None,
              status="ok", reason=None, http_status=200, domain=DOMAIN):
    """A scrape_one(report_mode=True) result."""
    cookies = list(cookies)
    analysis = fp.summarise_records(cookies)
    form_gap = form or {"has_data_form": False, "field_categories": [], "consent_checkbox": None,
                        "privacy_link": None, "external_hosts": []}
    score = fp.consent_score(cmp is not None, analysis, banner, policy, form_gap, evidence)
    return {
        "cin": "partner:0", "company_name": "Acme", "domain": domain,
        "final_url": f"https://{domain}/", "final_domain": domain, "http_status": http_status,
        "scan_status": status, "unscannable_reason": reason,
        "has_cmp": cmp is not None, "cmp_name": cmp, "cmp_evidence": evidence,
        "has_cookie_banner": banner, "has_privacy_policy": policy,
        "pre_consent_cookies": cookies, "cookies_total": analysis["cookies_total"],
        "tracking_cookie_count": analysis["tracking_count"],
        "third_party_domains": analysis["third_party_domains"],
        "third_party_tracking_domains": analysis["third_party_domains"],
        "third_party_count": analysis["third_party_count"],
        "consent_gap_summary": s3.build_consent_summary(cmp, analysis, banner, policy, form_gap, None),
        "consent_gap_score": score,
        "has_data_form": form_gap["has_data_form"], "form_field_types": form_gap["field_categories"],
        "form_consent_checkbox": form_gap["consent_checkbox"], "form_privacy_link": form_gap["privacy_link"],
        "form_third_party_hosts": form_gap["external_hosts"],
    }


GA_ONLY_BANNER = dict(cookies=GA, banner=True, policy=True)
GA_ONE_COOKIE = dict(cookies=[cookie("_ga")], banner=True, policy=True)
CLEAN = dict(cookies=[], policy=True)
FORM_ONLY = dict(cookies=[], policy=True, form=FORM)
GA_NO_BANNER = dict(cookies=GA, banner=False, policy=True)
ADS_NO_BANNER = dict(cookies=ADS + GA, banner=False, policy=True)
ADS_NO_POLICY = dict(cookies=ADS + GA, banner=False, policy=False)
UNSCANNABLE_403 = dict(status="unscannable", reason="homepage returned HTTP 403", http_status=403)
UNSCANNABLE_404 = dict(status="unscannable", reason="homepage returned HTTP 404", http_status=404)
UNSCANNABLE_BOT = dict(status="unscannable", reason="homepage is a bot check or challenge page")
UNSCANNABLE_PARKED = dict(status="unscannable", reason="homepage is parked, a placeholder or unfinished")
