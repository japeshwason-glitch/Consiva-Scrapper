"""
One-page DPDP report: findings engine, summary text, wording, rendering.

    docker compose --profile reports run --rm reports python -m unittest tests.test_reports -v

Runs in the reports image (Jinja2, pypdf, Chromium). The render tests print a
real PDF through the production render path.
"""
import asyncio
import io
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import report_build as rb
import report_findings as rf

BANNED = ("violation", "non-compliant", "illegal", "unlawful", "breach", "fine", "penalty",
          "liable", "₹", "compliant", "Consent Manager", "data sent to", "guarantee",
          "ensures compliance", "2027")


def _pattern(term):
    # Whole words, case-insensitive: "fine" must not trip on "defined" or
    # "refined". The rupee sign is not a word character, so it matches as is.
    return re.escape(term) if term == "₹" else rf"\b{re.escape(term)}\b"


def lint(text):
    return [t for t in BANNED if re.search(_pattern(t), text, re.I)]


def ck(name, vendor, category="analytics", domain="example.in", party="first", lifetime=399):
    return {"name": name, "domain": domain, "party": party, "session": False,
            "lifetime_days": lifetime, "secure": False, "same_site": "Lax",
            "http_only": False, "category": category, "vendor": vendor}


def scan(**kw):
    base = {"final_domain": "example.in", "has_cmp": False, "cmp_name": None,
            "cmp_evidence": None, "has_cookie_banner": True, "has_privacy_policy": True,
            "cookies": [], "third_party_tracking_domains": [], "has_data_form": False,
            "form_field_types": [], "form_consent_checkbox": None, "form_privacy_link": None}
    return {**base, **kw}


GA = [ck("_ga", "Google Analytics"), ck("_gid", "Google Analytics", lifetime=1)]
META = [ck("_fbp", "Meta Pixel", "advertising"),
        ck("fr", "Meta Pixel", "advertising", domain="facebook.com", party="third")]


def codes(result):
    return [f["code"] for f in result["findings"]]


class Findings(unittest.TestCase):
    def test_nothing_found(self):
        r = rf.evaluate(scan())
        self.assertEqual(codes(r), [])
        self.assertEqual(r["score"], 100)
        self.assertEqual(r["summary"], "")

    def test_g1_g2_g3_g4(self):
        r = rf.evaluate(scan(cookies=GA + META, third_party_tracking_domains=["facebook.com"],
                             has_cookie_banner=False, has_privacy_policy=False))
        self.assertEqual(codes(r), ["G1", "G2", "G3", "G4"])
        self.assertEqual(r["score"], 37)      # 9 + 3 + 6 + 25 + 15 + 5 adv = 63
        self.assertEqual(r["band"], "priority")

    def test_g5_needs_real_cmp_evidence(self):
        for ev in ("script_src", "dom"):
            r = rf.evaluate(scan(has_cmp=True, cmp_name="CookieYes", cmp_evidence=ev, cookies=GA))
            self.assertEqual(codes(r), ["G5"], ev)
        r = rf.evaluate(scan(has_cmp=True, cmp_name="CookieYes", cmp_evidence="text", cookies=GA))
        self.assertEqual(codes(r), ["G1"])

    def test_g1_and_g5_are_exclusive(self):
        for ev in ("script_src", "dom", "text", None):
            r = rf.evaluate(scan(has_cmp=ev is not None, cmp_name="OneTrust" if ev else None,
                                 cmp_evidence=ev, cookies=GA, has_cookie_banner=False))
            self.assertFalse({"G1", "G5"} <= set(codes(r)), ev)

    def test_g2_g3_only_with_g1(self):
        r = rf.evaluate(scan(has_cmp=True, cmp_name="OneTrust", cmp_evidence="dom",
                             cookies=META, third_party_tracking_domains=["facebook.com"],
                             has_cookie_banner=False))
        self.assertEqual(codes(r), ["G5"])

    def test_g4_fires_on_form_alone(self):
        r = rf.evaluate(scan(has_privacy_policy=False, has_data_form=True,
                             form_field_types=["email", "name"], form_consent_checkbox=True))
        self.assertEqual(codes(r), ["G4"])
        r = rf.evaluate(scan(has_privacy_policy=False))
        self.assertEqual(codes(r), [])

    def test_a2_review(self):
        r = rf.evaluate(scan(has_data_form=True, form_field_types=["phone", "email", "name"],
                             form_consent_checkbox=False))
        self.assertEqual(codes(r), ["A2"])
        self.assertEqual(r["gap_count"], 0)
        self.assertEqual(r["reviews"][0]["observed"],
                         "Form collects name, email and phone with no consent checkbox")

    def test_necessary_and_unclassified_never_count(self):
        r = rf.evaluate(scan(cookies=[ck("__cf_bm", None, "necessary"),
                                      ck("SRM_X", None, "unclassified", "cdn.net", "third")],
                             has_cookie_banner=False, has_privacy_policy=True))
        self.assertEqual(codes(r), [])
        self.assertEqual(r["score"], 100)

    def test_caps_and_order(self):
        r = rf.evaluate(MAX_SCAN)
        self.assertEqual([g["code"] for g in r["gaps"]], ["G1", "G2", "G3", "G4"])
        self.assertLessEqual(len(r["gaps"]), rf.MAX_GAPS)
        self.assertEqual(len(r["reviews"]), 1)

    def test_cookie_groups_overflow(self):
        groups = rf.evaluate(MAX_SCAN)["gaps"][0]["cookie_groups"]
        self.assertEqual(len(groups), 4)
        self.assertEqual(groups[-1]["vendor"], "Other vendors")
        self.assertTrue(all(len(g["names"]) <= 3 for g in groups))
        self.assertTrue(all(not n.startswith("and ") for g in groups for n in g["names"]))
        self.assertTrue(any(g["more"] > 0 for g in groups))


class Summary(unittest.TestCase):
    def test_g1_g2_g3_g4(self):
        r = rf.evaluate(scan(cookies=GA + META, third_party_tracking_domains=["facebook.com"],
                             has_cookie_banner=False, has_privacy_policy=False))
        self.assertEqual(r["summary"],
            "We found 4 consent readiness gaps on the homepage of example.in. When the page "
            "loads, 4 tracking cookies from Google Analytics and Meta Pixel are set before the "
            "visitor takes any action, including cookies from 1 third-party domain. No consent "
            "notice is shown, and no privacy information is published on the site.")

    def test_g5_g4(self):
        r = rf.evaluate(scan(has_cmp=True, cmp_name="CookieYes", cmp_evidence="script_src",
                             cookies=GA, has_privacy_policy=False))
        self.assertEqual(r["summary"],
            "We found 2 consent readiness gaps on the homepage of example.in. A consent tool "
            "(CookieYes) is installed, but 2 tracking cookies from Google Analytics are still "
            "set before the visitor takes any action. No privacy information is published on "
            "the site.")

    def test_g1_only(self):
        r = rf.evaluate(scan(cookies=[ck("_ga", "Google Analytics")]))
        self.assertEqual(r["summary"],
            "We found 1 consent readiness gap on the homepage of example.in. When the page "
            "loads, 1 tracking cookie from Google Analytics is set before the visitor takes "
            "any action.")


# The longest real domain among PartnerImport rows (32 characters), checked
# against the CRM on 2026-10-06; it is the only name in the header.
LONGEST_DOMAIN = "mohammedkhanandsonsjewellers.com"
YT = "YouTube"          # internal vendor key; the catalogue prints "YouTube (embedded video)"

# The common severe profile: trackers incl. a third party, no banner, no
# policy, a contact form without a consent box -> G1 G2 G3 G4 + A2.
COMMON_SEVERE = scan(
    final_domain=LONGEST_DOMAIN, cookies=GA + META,
    third_party_tracking_domains=["facebook.com"], has_cookie_banner=False,
    has_privacy_policy=False, has_data_form=True,
    form_field_types=["email", "name", "phone"], form_consent_checkbox=False)

# The same plus a third vendor and a second third-party domain.
THIRD_VENDOR = {**COMMON_SEVERE,
                "cookies": GA + META + [ck("_hjSessionUser_1", "Hotjar", "session_replay")],
                "third_party_tracking_domains": ["facebook.com", "www.linkedin.com"],
                "form_field_types": ["email", "message", "name", "phone"]}

# Everything at once: 4 gaps + 1 review, 7 vendors, many cookies and 5 long
# third-party domains.
MAX_SCAN = scan(
    final_domain=LONGEST_DOMAIN,
    cookies=[ck(f"_ga_{i}ABCDEFGH", "Google Analytics") for i in range(6)]
            + [ck("_fbp", "Meta Pixel", "advertising"),
               ck("fr", "Meta Pixel", "advertising", "facebook.com", "third")]
            + [ck(n, "Hotjar", "session_replay") for n in
               ("_hjSessionUser_1234567", "_hjSession_1234567", "_hjAbsoluteSessionInProgress")]
            + [ck("_clck", "Microsoft Clarity", "session_replay"),
               ck("MUID", "Microsoft Clarity", "session_replay", "clarity.ms", "third")]
            + [ck("bcookie", "LinkedIn", "advertising", "linkedin.com", "third"),
               ck("__hstc", "HubSpot"), ck("YSC", YT, "advertising", "youtube.com", "third")],
    third_party_tracking_domains=["facebook.com", "c.clarity.ms", "www.linkedin.com",
                                  "youtube.com", "doubleclick-analytics-partner.net"],
    has_cookie_banner=False, has_privacy_policy=False, has_data_form=True,
    form_field_types=["address", "company", "email", "message", "name", "phone"],
    form_consent_checkbox=False, form_privacy_link=False)


def render(scan_):
    """The production path: render_fitted(), dropping the review / detail only to fit."""
    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                return await rb.render_fitted(browser, scan_["final_domain"],
                                              "30 September 2026", scan_)
            finally:
                await browser.close()
    return asyncio.run(go())


def pdf_text(pdf):
    """The PDF's text, with Plex's fi/ffi ligature glyphs folded back to letters."""
    import unicodedata
    from pypdf import PdfReader
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    return unicodedata.normalize("NFKC", text)


class Wording(unittest.TestCase):
    def test_lint_is_whole_word(self):
        self.assertEqual(lint("defined, refined, finer, compliance"), [])
        self.assertEqual(lint("A Fine is possible"), ["fine"])
        self.assertEqual(lint("non-compliant"), ["non-compliant", "compliant"])

    def test_catalogue_is_clean(self):
        text = (rb.ROOT / "report_catalogue.yaml").read_text()
        text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertEqual(lint(text), [])

    def test_rendered_pdf_is_clean(self):
        pdf, why, _ = render(MAX_SCAN)
        self.assertIsNotNone(pdf, why)
        text = pdf_text(pdf)
        self.assertIn("DPDP Consent Readiness Report", text)
        self.assertEqual(lint(text), [])


class Rendering(unittest.TestCase):
    def _fits_keeping_every_gap(self, label, scan_):
        from pypdf import PdfReader
        pdf, why, fit = render(scan_)
        self.assertIsNotNone(pdf, why)                # nothing clipped
        self.assertEqual(len(PdfReader(io.BytesIO(pdf)).pages), 1)
        text = re.sub(r"\s+", " ", pdf_text(pdf))
        for gap in rf.evaluate(scan_)["gaps"]:       # never drop a gap
            self.assertIn(gap["sections"], text)
        print(f"\n  {label}: 1 page, fit = {fit}")
        return fit

    def test_common_severe_case(self):
        self.assertEqual(codes(rf.evaluate(COMMON_SEVERE)), ["G1", "G2", "G3", "G4", "A2"])
        self._fits_keeping_every_gap("common severe (4 gaps + review)", COMMON_SEVERE)

    def test_third_vendor_and_second_domain(self):
        self._fits_keeping_every_gap("third vendor + second domain", THIRD_VENDOR)

    def test_maximum_content_fits_one_page(self):
        self._fits_keeping_every_gap("extreme worst case", MAX_SCAN)

    def test_header_names_the_domain_only(self):
        tpl = rb.TEMPLATE_PATH.read_text()
        self.assertNotIn("company_name", tpl)
        pdf, why, _ = render(COMMON_SEVERE)
        text = pdf_text(pdf)
        self.assertIn(LONGEST_DOMAIN, text.replace("\n", ""))
        self.assertIn("SPECIALLY PREPARED FOR", text)
        self.assertIn("Scanned 30 September 2026", text)

    def test_display_domain(self):
        for raw in ("https://www.Example.co.in/", "HTTP://example.co.in", "www.example.co.in/x"):
            self.assertEqual(rb.display_domain(raw), "example.co.in", raw)

    def test_overflow_count_is_not_mono(self):
        html = rb.render_html(LONGEST_DOMAIN, "1 October 2026", rf.evaluate(MAX_SCAN))
        self.assertRegex(html, r"</span> and \d+ more")
        self.assertNotIn('class="mono">and ', html)

    def test_cookie_list_format(self):
        r = rf.evaluate(scan(cookies=GA + META + [ck("_ga_X", "Google Analytics"),
                                                  ck("_ga_Y", "Google Analytics")]))
        html = rb.render_html("example.in", "1 October 2026", r)
        text = re.sub(r"<[^>]+>", "", re.search(r"set on page load:.*?</div>", html).group(0))
        self.assertEqual(text, "set on page load: Google Analytics: _ga, _gid, _ga_X and 1 more; "
                               "Meta Pixel: _fbp, fr")
        self.assertNotIn("(", text)

    def test_ibm_plex_is_embedded(self):
        from pypdf import PdfReader
        pdf, why, _ = render(scan(cookies=GA, has_cookie_banner=False))
        self.assertIsNotNone(pdf, why)
        fonts = {str(f.get_object()["/BaseFont"]) for p in PdfReader(io.BytesIO(pdf)).pages
                 for f in p["/Resources"]["/Font"].values()}
        self.assertTrue(any("IBMPlexSans" in f for f in fonts), fonts)
        self.assertTrue(any("IBMPlexMono" in f for f in fonts), fonts)
        self.assertFalse(any("Liberation" in f or "WenQuanYi" in f for f in fonts), fonts)

    def test_autoescape(self):
        evil = scan(cookies=[ck("<script>alert(1)</script>", "Google Analytics")],
                    has_cookie_banner=False)
        html = rb.render_html("example.in", "1 October 2026", rf.evaluate(evil))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


# --- one source of truth: report score == CRM GapScore ------------------------
import fingerprints as fp
import partner_import_analyse as pia
from importlib import import_module
s3 = import_module("stages.03_scrape_playwright") if False else pia._stage03

DAY = 86400


def raw_cookie(name, domain, days=399):
    import time
    return {"name": name, "domain": domain, "expires": time.time() + days * DAY + 60,
            "secure": False, "httpOnly": False, "sameSite": "Lax"}


def form_raw(fields, consent=False, privacy=False, action_host="", external=False):
    """A form as stage 03's FORM_JS describes it."""
    return {"fields": fields, "has_consent_checkbox": consent, "has_privacy_link": privacy,
            "action_host": action_host, "external_action": external}


def scrape_result(site, raw_cookies, forms, banner, policy, cmp_name=None,
                  cmp_evidence=None, embed=None):
    """
    What scrape_one() computes for these inputs -- the same calls, in the same
    order, as the inline consent block of stages/03_scrape_playwright.py.
    """
    analysis = fp.analyse_cookies(raw_cookies, site)
    form = fp.form_gap(forms)
    has_cmp = cmp_name is not None
    for r in analysis["records"]:
        r["vendor"] = fp.cookie_vendor(r["name"], r["domain"], r["category"])
    return {
        "final_url": f"https://{site}/", "final_domain": site, "page_title": site,
        "has_cmp": has_cmp, "cmp_name": cmp_name, "cmp_evidence": cmp_evidence,
        "has_cookie_banner": banner, "has_privacy_policy": policy,
        "pre_consent_cookies": analysis["records"],
        "cookies_total": analysis["cookies_total"],
        "tracking_cookie_count": analysis["tracking_count"],
        "third_party_domains": analysis["third_party_domains"],
        "third_party_count": analysis["third_party_count"],
        "max_cookie_lifetime_days": analysis["max_lifetime_days"],
        "consent_gap_summary": s3.build_consent_summary(cmp_name, analysis, banner, policy, form, embed),
        "consent_gap_score": fp.consent_score(has_cmp, analysis, banner, policy, form,
                                              cmp_evidence),
        "has_data_form": form["has_data_form"], "form_field_types": form["field_categories"],
        "form_consent_checkbox": form["consent_checkbox"],
        "form_third_party_hosts": form["external_hosts"], "form_embed_provider": embed,
        "http_status": 200, "scan_status": "ok", "unscannable_reason": None,
        "form_privacy_link": form["privacy_link"],
        "third_party_tracking_domains": analysis["third_party_domains"],
    }


SITE = "shop.example.in"
SHAPES = {
    "G1 only": scrape_result(SITE, [raw_cookie("_ga", ".example.in")], [], True, True),
    "G1+G2+G3+G4": scrape_result(
        SITE, [raw_cookie("_ga", ".example.in"), raw_cookie("_gid", ".example.in", 1),
               raw_cookie("_fbp", ".example.in"), raw_cookie("fr", ".facebook.com"),
               raw_cookie("_hjSessionUser_9", ".example.in")], [], False, False),
    "G5": scrape_result(
        SITE, [raw_cookie("_ga", ".example.in"), raw_cookie("MUID", ".clarity.ms"),
               raw_cookie("CookieConsent", ".example.in")], [], True, True,
        cmp_name="Cookiebot", cmp_evidence="script_src"),
    "form only": scrape_result(
        SITE, [raw_cookie("PHPSESSID", "shop.example.in", 0)],
        [form_raw(["name", "email Email address", "phone"], action_host="forms.zoho.in",
                  external=True)], True, False, embed="Zoho Forms"),
    "clean": scrape_result(SITE, [raw_cookie("__cf_bm", ".example.in", 0)], [], True, True),
    "CMP, no trackers": scrape_result(
        SITE, [raw_cookie("CookieConsent", ".example.in")], [], True, True,
        cmp_name="Cookiebot", cmp_evidence="dom"),
    "text-only CMP + trackers": scrape_result(
        SITE, [raw_cookie("_ga", ".example.in"), raw_cookie("MUID", ".clarity.ms"),
               raw_cookie("CookieConsent", ".example.in")], [], True, True,
        cmp_name="Cookiebot", cmp_evidence="text"),
}
# The G5 site with the CMP taken away entirely -- what "scored normally" means.
NO_CMP_TWIN = scrape_result(
    SITE, [raw_cookie("_ga", ".example.in"), raw_cookie("MUID", ".clarity.ms"),
           raw_cookie("CookieConsent", ".example.in")], [], True, True)
LEAD = {"Id": 1, "CompanyName": "Example", "Website": SITE}


class OneSourceOfTruth(unittest.TestCase):
    def test_report_score_equals_crm_gapscore(self):
        for label, data in SHAPES.items():
            row = rb.scan_row(LEAD, SITE, data, None, 1)
            self.assertEqual(rf.score(row), rb.crm_values(data)["GapScore"], label)
            self.assertEqual(rf.evaluate(row)["score"], data["consent_gap_score"], label)

    def test_shapes_cover_what_they_say(self):
        got = {k: codes(rf.evaluate(rb.scan_row(LEAD, SITE, d, None, 1))) for k, d in SHAPES.items()}
        self.assertEqual(got["G1 only"], ["G1"])
        self.assertEqual(got["G1+G2+G3+G4"], ["G1", "G2", "G3", "G4"])
        self.assertEqual(got["G5"], ["G5"])
        self.assertEqual(got["form only"], ["G4", "A2"])
        self.assertEqual(got["clean"], [])
        self.assertEqual(got["CMP, no trackers"], [])
        self.assertEqual(got["text-only CMP + trackers"], ["G1", "G2"])  # MUID is on clarity.ms

    def test_cmp_scoring_rules(self):
        normal = NO_CMP_TWIN["consent_gap_score"]
        self.assertLess(normal, 100)
        self.assertEqual(SHAPES["CMP, no trackers"]["consent_gap_score"], 100)
        self.assertEqual(SHAPES["G5"]["consent_gap_score"], normal)             # scored normally
        self.assertEqual(SHAPES["text-only CMP + trackers"]["consent_gap_score"], normal)
        print(f"\n  CMP+no trackers 100 | CMP+trackers {SHAPES['G5']['consent_gap_score']} | "
              f"text-only CMP+trackers {SHAPES['text-only CMP + trackers']['consent_gap_score']} "
              f"| same site without CMP {normal}")

    def test_unmeasured_evidence_keeps_the_old_rule(self):
        a = fp.summarise_records(SHAPES["G5"]["pre_consent_cookies"])
        self.assertEqual(fp.consent_score(True, a, True, True, None), 100)

    def test_crm_values_identical_to_partner_rescan(self):
        # The partner job and the partner rescan both build these with
        # values_for(); the report job must hand the CRM the very same values.
        for label, data in SHAPES.items():
            partner = pia.values_for({"ContactName": "", "CompanyScale": "1-50",
                                      "Industry": "Retail"}, data)
            self.assertEqual(rb.crm_values(data), {c: partner[c] for c in rb.CRM_SCAN_COLUMNS},
                             label)

    @unittest.skipUnless((rb.DATA / "partner_rescan_progress.jsonl").exists(),
                         "no partner rescan progress file on this machine")
    def test_crm_values_reproduce_real_rescan_writes(self):
        """
        For every real partner rescan row: rebuild ConsentFinding with today's
        builder and hand the scan to crm_values(). The six values must equal
        what the rescan wrote byte for byte -- except that an internal CMP
        label ("Moove GDPR (WP)") is now its display name ("Moove GDPR Consent").
        """
        import json
        sys.path.insert(0, str(rb.ROOT / "scripts"))
        from reclassify_cookies import form_from_row
        latest = {}
        for line in (rb.DATA / "partner_rescan_progress.jsonl").read_text().splitlines():
            e = json.loads(line)
            if e.get("ok"):
                latest[e["id"]] = e
        checked = relabelled = 0
        for e in latest.values():
            if e.get("rules") != fp.FINGERPRINTS_VERSION:
                continue
            sc = e["scan"]
            rebuilt = s3.build_consent_summary(
                sc["cmp_name"], fp.summarise_records(sc["pre_consent_cookies"]),
                sc["has_cookie_banner"], sc["has_privacy_policy"], form_from_row(sc),
                sc["form_embed_provider"])
            expected = dict(e["new"])
            if sc["cmp_name"] and fp.cmp_display_name(sc["cmp_name"]) != sc["cmp_name"]:
                expected["ConsentFinding"] = expected["ConsentFinding"].replace(
                    f"CMP detected: {sc['cmp_name']}",
                    f"CMP detected: {fp.cmp_display_name(sc['cmp_name'])}", 1)
                relabelled += 1
            self.assertEqual(rb.crm_values({**sc, "consent_gap_summary": rebuilt}), expected, e["id"])
            self.assertNotIn("(WP)", rebuilt, e["id"])
            checked += 1
        self.assertGreater(checked, 1000)
        print(f"\n  six CRM values reproduced for {checked} real rescan rows "
              f"({relabelled} differ only by a cleaned CMP name)")


class CrmWritesOnlyNineColumns(unittest.TestCase):
    def columns(self, sql):
        sets = re.search(r"SET (.*) WHERE", sql).group(1)
        return {c.split("=")[0].strip() for c in sets.split(",")}

    SIX = set(rb.CRM_SCAN_COLUMNS)

    def plan_sql(self, status, **kw):
        six = rb.crm_values(SHAPES["G1+G2+G3+G4"])
        w = rb.crm_write_plan(1, status, values=six, **kw)
        return w, rb.crm_update_sql(w)[0]

    def test_every_write_shape_stays_within_ten(self):
        for status, kw in (("generated", dict(url="https://x/r/a.pdf", set_url=True)),
                           ("no_gap", dict(url=None, set_url=True)),
                           ("unscannable", {}), ("failed", {})):
            _, sql = self.plan_sql(status, **kw)
            self.assertTrue(sql.startswith(f"UPDATE {rb.TABLE} SET"), sql)
            self.assertLessEqual(self.columns(sql), set(rb.CRM_WRITE_COLUMNS), status)
            self.assertIn("WHERE Id = %s AND Source = %s AND ScrapeStatus = 'done'", sql)
        self.assertEqual(len(rb.CRM_WRITE_COLUMNS), 10)
        self.assertIn("UpdatedAt", rb.CRM_WRITE_COLUMNS)

    def test_completed_scans_write_findings_and_updatedat(self):
        for status in ("generated", "no_gap"):
            _, sql = self.plan_sql(status, set_url=True)
            self.assertLessEqual(self.SIX, self.columns(sql), status)
            self.assertIn("UpdatedAt", self.columns(sql), status)

    def test_unscannable_and_failed_never_overwrite_findings(self):
        for status in ("unscannable", "failed"):
            w, sql = self.plan_sql(status)           # values offered, and refused
            self.assertIsNone(w["values"], status)
            self.assertEqual(self.columns(sql), {"ReportStatus", "ReportGeneratedAt"}, status)

    def test_unknown_column_is_refused(self):
        with self.assertRaises(ValueError):
            rb.crm_update_sql({"id": 1, "status": "x", "values": {"ContactName": "y"}})

    def test_no_other_sql_server_write_in_the_job(self):
        src = (rb.ROOT / "report_build.py").read_text()
        self.assertEqual(re.findall(r"\b(INSERT|DELETE|ALTER|MERGE|TRUNCATE|DROP)\b", src), [])
        # Exactly one UPDATE statement against the CRM table: crm_update_sql's.
        self.assertEqual(len(re.findall(r"UPDATE \{TABLE\}", src)), 1)
        self.assertNotRegex(src, r"UPDATE (dbo\.|\{config)")


# --- every customer-facing word comes from the catalogue ---------------------------
def catalogue_strings(node=rf.CATALOGUE):
    if isinstance(node, dict):
        return [x for v in node.values() for x in catalogue_strings(v)] + \
               [str(k) for k in node if isinstance(k, str)]
    if isinstance(node, list):
        return [x for v in node for x in catalogue_strings(v)]
    return [str(node)]


def template_static_text():
    t = rb.TEMPLATE_PATH.read_text()
    t = re.sub(r"\{#.*?#\}", " ", t, flags=re.S)
    t = re.sub(r"<style>.*?</style>", " ", t, flags=re.S)
    t = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", " ", t, flags=re.S)
    return re.sub(r"<[^>]+>", " ", t)


def words_of(*texts):
    return {w.lower() for t in texts for w in re.findall(r"[A-Za-z]{2,}", t)}


class AllWordingFromCatalogue(unittest.TestCase):
    def test_every_pdf_word_is_from_catalogue_template_or_scan(self):
        import config
        allowed = words_of(*catalogue_strings(), template_static_text(),
                           config.REPORT_SALES_CONTACT, "30 September 2026")
        for label, data in list(SHAPES.items()) + [("max", MAX_SCAN)]:
            row = rb.scan_row(LEAD, SITE, data, None, 1) if label != "max" else data
            if not rf.evaluate(row)["gaps"]:
                continue
            pdf, why, _ = render(row)
            self.assertIsNotNone(pdf, why)
            scan_words = words_of(row["final_domain"], *[c["name"] for c in row["cookies"]],
                                  *(row.get("third_party_tracking_domains") or []))
            stray = words_of(pdf_text(pdf)) - allowed - scan_words
            self.assertEqual(stray, set(), f"{label}: words not from the catalogue: {stray}")

    def test_findings_module_has_no_english_literals(self):
        import ast
        tree = ast.parse((rb.ROOT / "report_findings.py").read_text())
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef)) and n.body
                      and isinstance(n.body[0], ast.Expr)
                      and isinstance(n.body[0].value, ast.Constant)}
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in docstrings and re.search(r"[A-Za-z]+ [A-Za-z]+", n.value)]
        self.assertEqual(literals, [])

    def test_every_cmp_and_vendor_has_a_customer_name(self):
        for name, _ in fp.CMP_SIGNATURES:
            self.assertIn(name, rf.CATALOGUE["cmp_names"], name)
        vendors = {"Other"} | {v for _, v in fp.VENDOR_BY_PREFIX}
        for v in fp.VENDOR_BY_EXACT.values():
            vendors |= set(v.values()) if isinstance(v, dict) else {v}
        for v in vendors:
            self.assertIn(v, rf.CATALOGUE["vendor_names"], v)
        for shown in list(rf.CATALOGUE["cmp_names"].values()) + list(rf.CATALOGUE["vendor_names"].values()):
            self.assertNotIn("(WP)", shown)

    def test_crm_finding_names_match_the_catalogue(self):
        self.assertEqual(fp.CMP_DISPLAY_NAMES, rf.CATALOGUE["cmp_names"])
        self.assertEqual(set(fp.CMP_DISPLAY_NAMES), {n for n, _ in fp.CMP_SIGNATURES})
        a = fp.summarise_records([])
        text = s3.build_consent_summary("Moove GDPR (WP)", a, True, True)
        self.assertTrue(text.startswith("CMP detected: Moove GDPR Consent;"), text)
        self.assertNotIn("(WP)", text)

    def test_internal_cmp_label_never_prints(self):
        g5 = scan(has_cmp=True, cmp_name="Moove GDPR (WP)", cmp_evidence="dom",
                  cookies=GA + [ck("YSC", YT, "advertising", "youtube.com", "third")])
        pdf, why, _ = render(g5)
        text = re.sub(r"\s+", " ", pdf_text(pdf))
        self.assertIn("Moove GDPR Consent", text)
        self.assertIn("YouTube (embedded video)", text)
        self.assertNotIn("(WP)", text)


class HomepageUrls(unittest.TestCase):
    def test_default_path_unchanged(self):
        self.assertEqual(s3.homepage_urls("bisleri.com"),
                         ["https://bisleri.com", "http://bisleri.com"])

    def test_report_mode_falls_back_to_www(self):
        self.assertEqual(s3.homepage_urls("bisleri.com", www_fallback=True),
                         ["https://bisleri.com", "http://bisleri.com",
                          "https://www.bisleri.com", "http://www.bisleri.com"])
        self.assertEqual(s3.homepage_urls("www.x.in", www_fallback=True),
                         ["https://www.x.in", "http://www.x.in"])


class YouTubeOnly(unittest.TestCase):
    YT_ONLY = scan(cookies=[ck("YSC", YT, "advertising", "youtube.com", "third"),
                            ck("VISITOR_INFO1_LIVE", YT, "advertising", "youtube.com", "third")],
                   third_party_tracking_domains=["youtube.com"], has_cookie_banner=False)

    def test_note_only_when_every_tracker_is_youtube(self):
        self.assertEqual(rf.evaluate(self.YT_ONLY)["note"], rf.CATALOGUE["youtube_only_note"])
        mixed = {**self.YT_ONLY, "cookies": self.YT_ONLY["cookies"] + GA}
        self.assertEqual(rf.evaluate(mixed)["note"], "")
        self.assertEqual(rf.evaluate(scan())["note"], "")

    def test_note_prints_and_fits(self):
        pdf, why, fit = render({**self.YT_ONLY, "final_domain": LONGEST_DOMAIN})
        self.assertIsNotNone(pdf, why)
        text = re.sub(r"\s+", " ", pdf_text(pdf))
        self.assertIn("privacy-enhanced (no-cookie) embed avoids them", text)

    def test_youtube_only_leads_run_last(self):
        lead = lambda i, finding, trk, gap: {"Id": i, "ConsentFinding": finding,
                                             "TrackersPreConsent": trk, "GapScore": gap}
        yt = lead(1, "No consent management platform detected; 2 of 3 cookies are tracking "
                     "cookies set before consent: YSC, VISITOR_INFO1_LIVE; no cookie banner.", 2, 5)
        ga = lead(2, "No consent management platform detected; 1 of 1 cookies are tracking "
                     "cookies set before consent: _ga; no cookie banner.", 1, 90)
        form = lead(3, "No consent management platform detected; no tracking cookies set "
                       "before consent; no cookie banner.", 0, 75)
        self.assertTrue(rb.likely_youtube_only(yt))
        self.assertFalse(rb.likely_youtube_only(ga))
        self.assertEqual([l["Id"] for l in sorted([yt, ga, form], key=rb.run_order)], [2, 3, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
