"""
Auto Email Campaign, milestone 3, stage 9: drafting.

    SP=<scratchpad>; $SP/run.sh api python -m unittest tests.test_campaign_draft -v

The model is replaced by a fake generator (or a local fake HTTP server for the request-shape tests).
Nothing here calls OpenAI, the gignati server or the CRM. The template wording is a PLACEHOLDER until
the CEO and sales team approve it; these tests pin the structure, not the words.
"""
import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
import report_findings as rf

from campaign import (crm_write, draft_findings, draft_model, draft_review, draft_templates,
                      draft_validate, drafting, settings, settings_drafts, status)
from tests import campaign_fixtures as fx
from tests.test_campaign import DbCase, FakeCrmConn, crm_with, writer

REPORT_URL = "https://reports-consiva.gignati.com/r/0123456789abcdef0123456789abcdef.pdf"
FIRST_NAME = "Zorbatha"            # a name that cannot occur by accident in any generated text
EMAIL = "zorbatha.quimby@acme.example.com"


def scan_row(**fixture):
    """A stored report_scans-shaped dict for a fixture (what the DB hands the stage)."""
    from campaign import scan_adapter
    company = {"company_id": 1, "company_name": "Acme Textiles", "domain": "acme.example.com"}
    return scan_adapter.scan_row_from(company, fx.scan_data(**fixture), None, attempts=1)


def findings_for(fixture=None, **company):
    fixture = fixture or fx.ADS_NO_BANNER
    scan = scan_row(**fixture)
    scan["scanned_at"] = __import__("datetime").datetime(2026, 10, 7, 9, 0, tzinfo=__import__("datetime").timezone.utc)
    base = {"company_name": "Acme Textiles", "domain": "acme.example.com", "industry": "Textile manufacturing",
            "band": "B", "score": 46}
    base.update(company)
    return draft_findings.build_findings(scan, base)


GOOD = {"opening_line": "Acme Textiles makes fabric for garment makers.",
        "finding_sentence": "Some advertising cookies are set on your homepage before a visitor makes any choice."}


# --- the findings handed to the model --------------------------------------------------------------
class Findings(unittest.TestCase):
    def test_structured_findings_carry_the_facts_the_report_is_built_from(self):
        f = findings_for(fx.ADS_NO_BANNER)
        self.assertEqual(f["company_name"], "Acme Textiles")
        self.assertEqual(f["domain"], "acme.example.com")
        self.assertEqual(f["what_it_does"], "Textile manufacturing")
        self.assertEqual((f["band"], f["score"]), ("B", 46))
        self.assertEqual(f["gap_count"], 3)
        self.assertEqual(f["tracking_cookie_count"], 5)
        self.assertEqual(f["cookie_banner"], False)
        self.assertEqual(f["privacy_policy"], True)
        self.assertIn("Meta Pixel", f["tracker_vendors"])
        self.assertIn("facebook.com", f["third_party_domains"])
        self.assertEqual(f["top_finding"]["code"], "G1")
        self.assertEqual(f["scan_date"], "7 October 2026")

    def test_a_form_finding_is_included(self):
        f = findings_for(dict(cookies=fx.GA, banner=True, policy=False, form=fx.FORM))
        self.assertTrue(f["form"]["collects_data"])
        self.assertEqual(f["form"]["consent_checkbox"], False)
        self.assertIn("email", f["form"]["fields"])

    def test_what_the_company_does_is_optional(self):
        f = findings_for(fx.ADS_NO_BANNER, industry=None)
        self.assertIsNone(f["what_it_does"])

    def test_there_is_nothing_personal_in_the_findings(self):
        blob = json.dumps(findings_for(fx.ADS_NO_BANNER))
        self.assertNotIn("@", blob)
        for word in ("first_name", "email", "phone", "contact_name"):
            self.assertNotIn(word, blob)

    def test_allowed_numbers_are_the_numbers_in_the_findings(self):
        nums = draft_findings.allowed_numbers(findings_for(fx.ADS_NO_BANNER))
        for n in ("5", "3", "46", "7", "2026", "2"):
            self.assertIn(n, nums, n)
        self.assertNotIn("11", nums)

    def test_allowed_names_hold_vendors_domains_and_the_company(self):
        names = draft_findings.allowed_names(findings_for(fx.ADS_NO_BANNER))
        for n in ("meta pixel", "google ads", "google analytics", "facebook.com", "doubleclick.net",
                  "acme textiles", "acme.example.com"):
            self.assertIn(n, names, n)
        self.assertNotIn("hotjar", names)


# --- validation: every rule rejects a bad slot --------------------------------------------------------
class Validation(unittest.TestCase):
    def setUp(self):
        self.f = findings_for(fx.ADS_NO_BANNER)

    def fails(self, slot, text):
        return draft_validate.validate_slot(slot, text, self.f)

    def test_a_good_pair_passes(self):
        self.assertEqual(draft_validate.validate(GOOD, self.f), {})

    def test_a_number_not_in_the_findings_is_rejected(self):
        self.assertIn("number", self.fails("finding_sentence", "Eleven trackers fire before any choice is made on 11 pages."))
        self.assertIn("number", self.fails("finding_sentence", "About 40 cookies are set before a visitor chooses anything."))

    def test_a_number_that_is_in_the_findings_is_allowed(self):
        self.assertEqual(self.fails("finding_sentence", "Five tracking cookies are set before a visitor makes any choice."), [])
        self.assertEqual(self.fails("finding_sentence", "5 tracking cookies are set before a visitor makes any choice."), [])

    def test_number_words_are_checked_too(self):
        self.assertIn("number", self.fails("finding_sentence", "Nine tracking cookies are set before a visitor makes any choice."))

    def test_a_vendor_that_is_not_in_the_findings_is_rejected(self):
        self.assertIn("name", self.fails("finding_sentence", "Hotjar and Mixpanel set cookies before a visitor makes any choice."))
        self.assertIn("name", self.fails("finding_sentence", "Cookiebot is not installed, so cookies fire before any choice."))

    def test_a_vendor_that_is_in_the_findings_is_allowed(self):
        self.assertEqual(self.fails("finding_sentence", "Meta Pixel and Google Ads cookies are set before a visitor makes any choice."), [])

    def test_a_domain_that_is_not_in_the_findings_is_rejected(self):
        self.assertIn("name", self.fails("finding_sentence", "Data goes to tracker.example.org before a visitor makes any choice."))

    def test_a_domain_that_is_in_the_findings_is_allowed(self):
        self.assertEqual(self.fails("finding_sentence", "Data goes to facebook.com before a visitor makes any choice on the page."), [])

    def test_the_banned_words_are_rejected_as_whole_words(self):
        for word in ("violation", "fine", "penalty", "deadline", "illegal", "lawsuit", "non-compliant", "unlawful"):
            self.assertIn("banned_word", self.fails("finding_sentence", f"This is a {word} for your homepage and visitors there."), word)
        self.assertEqual(self.fails("finding_sentence", "A refined set-up would ask first before any cookies are set on the page."), [])
        self.assertEqual(self.fails("finding_sentence", "Your site is well defined and sets cookies before any choice is made."), [])

    def test_the_banned_list_is_a_config_value(self):
        with mock.patch.object(settings_drafts, "BANNED_WORDS", ("cookies",)):
            self.assertIn("banned_word", self.fails("finding_sentence", "Some cookies are set before a visitor makes any choice."))

    def test_length_limits_per_slot(self):
        self.assertIn("length", self.fails("opening_line", "Short."))
        self.assertIn("length", self.fails("opening_line", "Acme Textiles " + "makes fabric " * 30 + "."))
        self.assertIn("length", self.fails("finding_sentence", "x " * 200 + "."))
        self.assertEqual(self.fails("opening_line", "Acme Textiles makes fabric for garment makers."), [])

    def test_empty_and_non_string_slots_are_rejected(self):
        for bad in ("", "   ", None, 5, ["a"]):
            self.assertTrue(self.fails("opening_line", bad), repr(bad))

    def test_one_sentence_only(self):
        self.assertIn("sentences", self.fails("finding_sentence", "Cookies are set early. They fire before a visitor can choose."))
        self.assertIn("sentences", self.fails("finding_sentence", "Cookies are set early on your homepage\nbefore a visitor chooses anything."))
        self.assertIn("sentences", self.fails("finding_sentence", "Cookies are set before a visitor makes any choice on the page"))

    def test_no_urls(self):
        for t in ("See https://example.com for the cookies set before a visitor makes any choice.",
                  "See www.example.com for the cookies set before a visitor makes any choice.",
                  "Cookies are listed at acme.example.com/report before a visitor makes any choice."):
            self.assertIn("url", self.fails("finding_sentence", t), t)

    def test_no_email_addresses(self):
        self.assertIn("email", self.fails("opening_line", "Write to someone@acme.example.com about the fabric Acme Textiles makes."))

    def test_no_markup(self):
        self.assertIn("markup", self.fails("opening_line", "Acme Textiles makes <b>fabric</b> for garment makers."))
        self.assertIn("markup", self.fails("opening_line", "Acme Textiles makes fabric & garments for the makers."))

    def test_no_legal_claims(self):
        for t in ("Your cookies are required by law to be switched off until a visitor makes a choice.",
                  "The DPDP Act says cookies must wait for a visitor to make a choice on the page.",
                  "Under the regulation your homepage must ask before any cookies are set on the page."):
            self.assertIn("legal_claim", self.fails("finding_sentence", t), t)

    def test_each_failure_is_reported_per_slot(self):
        res = draft_validate.validate({"opening_line": "Hi.", "finding_sentence": GOOD["finding_sentence"]}, self.f)
        self.assertEqual(set(res), {"opening_line"})

    def test_missing_slots_are_failures(self):
        self.assertEqual(set(draft_validate.validate({"opening_line": GOOD["opening_line"]}, self.f)), {"finding_sentence"})

    def test_extra_slots_are_ignored_not_trusted(self):
        res = draft_validate.validate({**GOOD, "subject": "Free money"}, self.f)
        self.assertEqual(res, {})


class Fallbacks(unittest.TestCase):
    def test_every_fallback_passes_every_validation_rule(self):
        for fixture in (fx.ADS_NO_BANNER, fx.GA_NO_BANNER, fx.GA_ONLY_BANNER, fx.ADS_NO_POLICY,
                        dict(cookies=[], banner=False, policy=False, form=fx.FORM)):
            f = findings_for(fixture)
            slots = draft_templates.fallback_slots(f)
            self.assertEqual(draft_validate.validate(slots, f), {}, fixture)

    def test_a_fallback_needs_no_model_and_no_industry(self):
        f = findings_for(fx.ADS_NO_BANNER, industry=None)
        slots = draft_templates.fallback_slots(f)
        self.assertTrue(slots["opening_line"] and slots["finding_sentence"])

    def test_the_finding_fallback_follows_the_top_finding(self):
        g1 = draft_templates.fallback_slots(findings_for(fx.ADS_NO_BANNER))["finding_sentence"]
        g5 = draft_templates.fallback_slots(findings_for(dict(cookies=fx.GA, cmp="OneTrust", evidence="script_src", banner=True)))["finding_sentence"]
        self.assertNotEqual(g1, g5)


# --- templates: fixed parts, per band, versioned ---------------------------------------------------------
class Templates(unittest.TestCase):
    def render(self, band="B", **kw):
        args = dict(opening_line=GOOD["opening_line"], finding_sentence=GOOD["finding_sentence"],
                    first_name=FIRST_NAME, report_url=REPORT_URL, domain="acme.example.com")
        args.update(kw)
        return draft_templates.render(band, **args)

    def test_there_is_one_template_file_per_band_in_the_templates_folder(self):
        for band in "ABCD":
            self.assertTrue((ROOT / "templates" / "email" / f"band_{band.lower()}.html.j2").exists(), band)

    def test_every_template_is_marked_as_a_placeholder_and_versioned(self):
        for band in "ABCD":
            text = (ROOT / "templates" / "email" / f"band_{band.lower()}.html.j2").read_text()
            self.assertIn("PLACEHOLDER", text)
            r = self.render(band)
            self.assertIn("placeholder", r.template_version.lower())
            self.assertTrue(r.template_version.startswith(band))

    def test_the_subject_is_fixed_in_the_template_per_band(self):
        subjects = {b: self.render(b).subject for b in "ABCD"}
        self.assertTrue(all(subjects.values()))
        self.assertEqual(subjects["B"], self.render("B", opening_line="Another opening line goes here for Acme.").subject)

    def test_the_report_link_is_descriptive_anchor_text_pointing_at_the_report_url(self):
        r = self.render()
        m = re.search(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', r.html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), REPORT_URL)
        self.assertGreater(len(m.group(2).split()), 2, "anchor text is a description, not a bare URL")
        self.assertNotIn("http", m.group(2))
        self.assertEqual(len(re.findall(r"<a ", r.html)), 1)

    def test_the_slots_and_first_name_land_in_the_body(self):
        r = self.render()
        self.assertIn(GOOD["opening_line"], r.html)
        self.assertIn(GOOD["finding_sentence"], r.html)
        self.assertIn(FIRST_NAME, r.html)
        self.assertIn(FIRST_NAME, r.text)

    def test_a_missing_first_name_gives_a_plain_greeting(self):
        r = self.render(first_name=None)
        self.assertNotIn("None", r.html)
        self.assertIn("Hello", r.html)

    def test_the_plain_text_version_has_the_link_and_no_markup(self):
        r = self.render()
        self.assertIn(REPORT_URL, r.text)
        self.assertNotIn("<", r.text)
        self.assertIn(GOOD["finding_sentence"], r.text)

    def test_the_fixed_parts_are_identical_whatever_the_slots_say(self):
        """Structure, CTA, closing, subject and the link do not move when the model's text does."""
        a = self.render(opening_line="Alpha line about the business of Acme Textiles here.",
                        finding_sentence="Some advertising cookies fire on your homepage before any choice.")
        b = self.render(opening_line="Beta line about the business of Acme Textiles there.",
                        finding_sentence="A few analytics cookies fire on your homepage before any choice.")
        strip = lambda r, *slots: re.sub("|".join(re.escape(s) for s in slots), "{slot}", r.html)
        self.assertEqual(strip(a, "Alpha line about the business of Acme Textiles here.",
                               "Some advertising cookies fire on your homepage before any choice."),
                         strip(b, "Beta line about the business of Acme Textiles there.",
                               "A few analytics cookies fire on your homepage before any choice."))
        self.assertEqual(a.subject, b.subject)

    def test_a_slot_cannot_inject_markup_even_if_validation_were_skipped(self):
        r = self.render(opening_line='</p><a href="https://evil.example.com">x</a>')
        self.assertNotIn("evil.example.com", re.findall(r'href="([^"]+)"', r.html))
        self.assertEqual(re.findall(r'href="([^"]+)"', r.html), [REPORT_URL])

    def test_bands_differ_in_their_fixed_wording(self):
        bodies = {b: re.sub(r"\s+", " ", self.render(b).html) for b in "ABCD"}
        self.assertEqual(len(set(bodies.values())), 4)

    def test_the_template_version_changes_when_the_template_file_changes(self):
        v1 = draft_templates.template_version("A")
        path = ROOT / "templates" / "email" / "band_a.html.j2"
        with mock.patch.object(draft_templates, "_source", lambda band: path.read_text() + "\n{# edited #}"):
            self.assertNotEqual(draft_templates.template_version("A"), v1)

    def test_an_unknown_band_is_refused(self):
        with self.assertRaises(ValueError):
            self.render("E")


# --- the model request: provider, shape, data minimisation ----------------------------------------------
class ModelRequest(unittest.TestCase):
    def setUp(self):
        self.f = findings_for(fx.ADS_NO_BANNER)
        self.payload = draft_model.build_payload(self.f, "ceo")

    def test_the_payload_has_only_the_agreed_fields(self):
        self.assertEqual(set(self.payload), {"company_name", "domain", "what_it_does", "band", "score", "gap_count",
                                             "scan_date", "findings", "contact_function"})
        self.assertEqual(self.payload["contact_function"], "ceo")

    def test_no_contact_name_email_phone_or_raw_title_is_in_the_payload(self):
        blob = json.dumps(self.payload)
        self.assertNotIn(FIRST_NAME, blob)
        self.assertNotIn("@", blob)
        self.assertNotIn("quimby", blob.lower())
        self.assertIsNone(re.search(r"\+?\d[\d\s-]{8,}", blob), "no phone-like number")

    def test_the_openai_request_is_structured_outputs_with_a_strict_schema(self):
        url, headers, body = draft_model.build_request("openai", self.payload, key="sk-test")
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        rf_ = body["response_format"]
        self.assertEqual(rf_["type"], "json_schema")
        self.assertTrue(rf_["json_schema"]["strict"])
        schema = rf_["json_schema"]["schema"]
        self.assertEqual(set(schema["required"]), {"opening_line", "finding_sentence"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(body["model"], settings_drafts.OPENAI_MODEL)
        self.assertLessEqual(body["temperature"], 0.5)
        self.assertLessEqual(body["max_completion_tokens"], 300)
        self.assertNotIn("chat_template_kwargs", body)

    def test_the_gignati_request_switches_thinking_off(self):
        url, headers, body = draft_model.build_request("gignati", self.payload)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("Authorization", headers)

    def test_the_system_prompt_is_byte_identical_across_requests(self):
        a = draft_model.build_request("openai", draft_model.build_payload(self.f, "ceo"), key="k")[2]["messages"][0]
        b = draft_model.build_request("openai", draft_model.build_payload(findings_for(fx.GA_NO_BANNER), "marketing"), key="k")[2]["messages"][0]
        self.assertEqual(a, b)
        self.assertEqual(a["content"], draft_model.SYSTEM_PROMPT)

    def test_the_user_message_is_the_payload_as_json_and_nothing_else(self):
        body = draft_model.build_request("openai", self.payload, key="k")[2]
        self.assertEqual(json.loads(body["messages"][1]["content"]), self.payload)
        self.assertEqual(len(body["messages"]), 2)

    def test_the_key_never_appears_in_the_body(self):
        body = draft_model.build_request("openai", self.payload, key="sk-secret-123")[2]
        self.assertNotIn("sk-secret-123", json.dumps(body))

    def test_the_provider_is_a_config_value_and_defaults_to_openai(self):
        self.assertEqual(settings_drafts.PROVIDER, "openai")
        self.assertEqual(set(settings_drafts.PROVIDERS), {"openai", "gignati", "mock"})
        with self.assertRaises(ValueError):
            draft_model.build_request("nope", self.payload)

    def test_a_missing_openai_key_is_a_clean_unavailable_error(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(draft_model.ModelUnavailable) as cm:
                draft_model.generate(self.payload, provider="openai")
        self.assertIn("OPENAI_API_KEY", str(cm.exception))


class FakeServer:
    """A local HTTP server standing in for either provider."""

    def __init__(self, responder):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                code, payload = responder(len(outer.requests), body)
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def chat_reply(slots, pt=300, ct=60):
    return 200, {"choices": [{"message": {"content": json.dumps(slots)}}], "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


class ModelCall(unittest.TestCase):
    def setUp(self):
        self.payload = draft_model.build_payload(findings_for(fx.ADS_NO_BANNER), "ceo")

    def with_server(self, responder, **env):
        srv = FakeServer(responder)
        self.addCleanup(srv.close)
        patches = [mock.patch.object(settings_drafts, "OPENAI_BASE_URL", srv.url),
                   mock.patch.object(settings_drafts, "GIGNATI_BASE_URL", srv.url.removesuffix("/v1")),
                   mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test", **env})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return srv

    def test_a_good_reply_returns_the_slots_and_the_token_usage(self):
        srv = self.with_server(lambda n, b: chat_reply(GOOD, 321, 45))
        slots, usage = draft_model.generate(self.payload, provider="openai")
        self.assertEqual(slots, GOOD)
        self.assertEqual(usage, {"input_tokens": 321, "output_tokens": 45})
        self.assertEqual(srv.requests[0]["headers"]["Authorization"], "Bearer sk-test")

    def test_unparseable_output_is_returned_as_no_slots_not_an_exception(self):
        self.with_server(lambda n, b: (200, {"choices": [{"message": {"content": "not json"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}))
        slots, usage = draft_model.generate(self.payload, provider="openai")
        self.assertIsNone(slots)
        self.assertEqual(usage["input_tokens"], 9)

    def test_transient_errors_are_retried_then_raise_unavailable(self):
        srv = self.with_server(lambda n, b: (503, {"error": "busy"}))
        with mock.patch.object(settings_drafts, "RETRY_WAIT_S", 0):
            with self.assertRaises(draft_model.ModelUnavailable):
                draft_model.generate(self.payload, provider="openai")
        self.assertEqual(len(srv.requests), settings_drafts.HTTP_RETRIES + 1)

    def test_a_429_then_success_succeeds(self):
        self.with_server(lambda n, b: (429, {}) if n == 1 else chat_reply(GOOD))
        with mock.patch.object(settings_drafts, "RETRY_WAIT_S", 0):
            slots, _ = draft_model.generate(self.payload, provider="openai")
        self.assertEqual(slots, GOOD)

    def test_a_401_is_unavailable_and_not_retried(self):
        srv = self.with_server(lambda n, b: (401, {"error": "bad key"}))
        with self.assertRaises(draft_model.ModelUnavailable):
            draft_model.generate(self.payload, provider="openai")
        self.assertEqual(len(srv.requests), 1)

    def test_the_request_on_the_wire_has_no_personal_data(self):
        srv = self.with_server(lambda n, b: chat_reply(GOOD))
        draft_model.generate(self.payload, provider="openai")
        wire = json.dumps(srv.requests[0]["body"])
        self.assertNotIn(FIRST_NAME, wire)
        self.assertNotIn("@", wire)

    def test_the_mock_provider_makes_no_network_call_and_returns_valid_slots(self):
        f = findings_for(fx.ADS_NO_BANNER)
        slots, usage = draft_model.generate(draft_model.build_payload(f, "ceo"), provider="mock")
        self.assertEqual(draft_validate.validate(slots, f), {})
        self.assertEqual(usage, {"input_tokens": 0, "output_tokens": 0})

    def test_the_price_estimate_uses_the_configured_prices(self):
        with mock.patch.object(settings_drafts, "PRICE_PER_M_INPUT", 0.40), mock.patch.object(settings_drafts, "PRICE_PER_M_OUTPUT", 1.60):
            self.assertAlmostEqual(draft_model.estimate_cost(1_000_000, 1_000_000), 2.00)
            self.assertAlmostEqual(draft_model.estimate_cost(500, 100), 0.0004 * 0.5 + 0.0016 * 0.1, places=7)


# --- the stage -------------------------------------------------------------------------------------------
class DraftCase(DbCase):
    def verified(self, company_id=1, is_test=False, decision="OK", source="live", fixture=None, band="B",
                 report_age_days=1, first_name=FIRST_NAME, title_function="ceo", report_url=REPORT_URL, **kw):
        """A company at `verified` with a stored scan, a report and a verified chosen contact."""
        fixture = fixture or fx.ADS_NO_BANNER
        domain = kw.get("domain", f"c{company_id}.example.com")
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, domain_source, status, company_name,
                               industry, score, gap_count, band, report_url, report_date, risky)
                           VALUES (%s, %s, %s, 'website', 'verified', %s, %s, 46, 3, %s, %s,
                                   NOW() - make_interval(days => %s), %s)""",
                        (company_id, is_test, domain, f"Company {company_id}", kw.get("industry", "Textile manufacturing"),
                         band, report_url, report_age_days, decision == "RISKY"))
        from campaign import scan_adapter
        company = {"company_id": company_id, "company_name": f"Company {company_id}", "domain": domain}
        sid = db.insert_report_scan(**scan_adapter.scan_row_from(company, fx.scan_data(domain=domain, **fixture), None, 1))
        cid = f"contact-{company_id}"
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, job_title, first_name, check_passed,
                               rank, title_function, nb_decision, nb_source, nb_verdict)
                           VALUES (%s, %s, %s, 'Chief Executive Officer', %s, TRUE, 1, %s, %s, %s, 'valid')""",
                        (cid, company_id, f"zorbatha.quimby@c{company_id}.example.com", first_name, title_function, decision, source))
            cur.execute("UPDATE ae_companies SET scan_id = %s, chosen_contact_id = %s WHERE company_id = %s", (sid, cid, company_id))
        return cid

    def draft_row(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_drafts WHERE company_id = %s", (company_id,))
            return cur.fetchone()

    def run_stage(self, generator=None, **kw):
        w, patch = writer()
        gen = generator or (lambda payload: (GOOD, {"input_tokens": 400, "output_tokens": 50}))
        with patch:
            counts = drafting.run(generator=gen, **kw)
        return counts, w

    def company_status(self, company_id):
        return self.status_of(company_id)[0]


class DraftingGate(DraftCase):
    def test_a_verified_company_with_a_fresh_report_is_drafted(self):
        self.verified(1)
        counts, _ = self.run_stage()
        self.assertEqual(self.company_status(1), "drafted")
        self.assertEqual(counts["drafted"], 1)

    def test_only_verified_companies_are_considered(self):
        for cid, st in ((2, "contact_ranked"), (3, "reported"), (4, "suppressed"), (5, "drafted")):
            self.add_company(cid, st)
        called = []
        self.run_stage(generator=lambda p: called.append(1) or (GOOD, {"input_tokens": 1, "output_tokens": 1}))
        self.assertEqual(called, [])

    def test_a_report_older_than_14_days_blocks_the_draft(self):
        self.verified(1, report_age_days=15)
        counts, _ = self.run_stage()
        self.assertEqual(self.company_status(1), "verified")
        self.assertEqual(counts["blocked"], {"report_stale": 1})
        self.assertIsNone(self.draft_row(1))

    def test_a_report_of_exactly_14_days_is_still_fresh(self):
        self.verified(1, report_age_days=13)
        self.run_stage()
        self.assertEqual(self.company_status(1), "drafted")

    def test_a_company_without_a_report_url_is_blocked(self):
        self.verified(1, report_url=None)
        counts, _ = self.run_stage()
        self.assertEqual(self.company_status(1), "verified")
        self.assertEqual(counts["blocked"], {"no_report": 1})

    def test_a_chosen_contact_suppressed_since_verification_stops_the_company(self):
        self.verified(1)
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES ('zorbatha.quimby@c1.example.com', 'unsubscribed', 'webhook')")
        counts, _ = self.run_stage()
        self.assertEqual(self.company_status(1), "suppressed")
        self.assertIsNone(self.draft_row(1))
        self.assertEqual(counts["suppressed"], 1)

    def test_suppression_matching_ignores_case(self):
        self.verified(1)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_contacts SET email = 'Zorbatha.Quimby@C1.Example.com'")
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES ('zorbatha.quimby@c1.example.com', 'bounced', 'x')")
        self.run_stage()
        self.assertEqual(self.company_status(1), "suppressed")

    def test_a_contact_that_is_not_ok_or_risky_is_blocked(self):
        self.verified(1, decision="NOT_CHECKED")
        counts, _ = self.run_stage()
        self.assertEqual(self.company_status(1), "verified")
        self.assertEqual(counts["blocked"], {"contact_not_verified": 1})

    def test_the_daily_cap_stops_the_stage(self):
        for i in range(1, 6):
            self.verified(i)
        with mock.patch.object(settings, "DRAFTS_PER_DAY", 3):
            counts, _ = self.run_stage()
        self.assertEqual(self.count("ae_drafts"), 3)
        self.assertEqual(counts["stopped"], "daily_cap")
        with mock.patch.object(settings, "DRAFTS_PER_DAY", 3):
            self.run_stage()
        self.assertEqual(self.count("ae_drafts"), 3, "the cap is per day, across runs")

    def test_dry_run_calls_no_model_and_writes_nothing(self):
        self.verified(1)
        self.verified(2, report_age_days=20)
        called = []
        counts, _ = self.run_stage(generator=lambda p: called.append(1), dry_run=True)
        self.assertEqual(called, [])
        self.assertEqual(self.count("ae_drafts"), 0)
        self.assertEqual((counts["would_draft"], counts["blocked"]), (1, {"report_stale": 1}))

    def test_limit_is_respected(self):
        for i in range(1, 6):
            self.verified(i)
        self.run_stage(limit=2)
        self.assertEqual(self.count("ae_drafts"), 2)


class MockVerdictsNeverDraftInProduction(DraftCase):
    def test_a_production_company_with_a_mock_verdict_is_not_drafted(self):
        self.verified(1, source="mock")
        counts, _ = self.run_stage()
        self.assertEqual(self.company_status(1), "verified")
        self.assertEqual(counts["blocked"], {"mock_verdict": 1})
        self.assertIsNone(self.draft_row(1))

    def test_an_unknown_source_is_treated_like_mock(self):
        self.verified(1, source=None)
        self.run_stage()
        self.assertEqual(self.company_status(1), "verified")

    def test_a_production_company_with_a_live_verdict_is_drafted(self):
        self.verified(1, source="live")
        self.run_stage()
        self.assertEqual(self.company_status(1), "drafted")

    def test_the_drafting_function_itself_refuses_a_mock_verdict(self):
        """The guard is inside draft_one, not only in the loop that calls it."""
        self.verified(1, source="mock")
        with self.assertRaises(drafting.MockVerdictRefused):
            with db.get_connection() as conn:
                with conn.cursor(cursor_factory=__import__("psycopg2.extras").extras.RealDictCursor) as cur:
                    drafting.draft_one(cur, self.company_row(1), lambda p: (GOOD, {"input_tokens": 1, "output_tokens": 1}))
        self.assertIsNone(self.draft_row(1))
        self.assertEqual(self.company_status(1), "verified")

    def test_the_test_cohort_may_use_a_mock_verdict(self):
        self.verified(1, is_test=True, source="mock")
        self.run_stage(only_test=True)
        self.assertEqual(self.company_status(1), "drafted")

    def company_row(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_companies WHERE company_id = %s", (company_id,))
            return cur.fetchone()


class DraftOutput(DraftCase):
    def test_the_draft_is_stored_with_everything_the_review_needs(self):
        self.verified(1)
        self.run_stage()
        d = self.draft_row(1)
        self.assertEqual(d["to_email"], "zorbatha.quimby@c1.example.com")
        self.assertEqual(d["contact_id"], "contact-1")
        self.assertEqual(d["band"], "B")
        self.assertTrue(d["subject"])
        self.assertIn(GOOD["opening_line"], d["body_html"])
        self.assertIn(GOOD["finding_sentence"], d["plain_text"])
        self.assertIn(FIRST_NAME, d["body_html"])
        self.assertIn(REPORT_URL, d["body_html"])
        self.assertIn("placeholder", d["template_version"].lower())
        self.assertEqual(d["prompt_version"], settings_drafts.PROMPT_VERSION)
        self.assertEqual(d["provider"], settings_drafts.PROVIDER)
        self.assertEqual(d["model"], settings_drafts.model_for(settings_drafts.PROVIDER))
        self.assertEqual(d["slot_source"], {"opening_line": "model", "finding_sentence": "model"})
        self.assertEqual((d["input_tokens"], d["output_tokens"], d["attempts"]), (400, 50, 1))
        self.assertIsNotNone(d["generated_at"])
        self.assertEqual(d["findings"]["tracking_cookie_count"], 5)
        self.assertEqual(d["contact_function"], "ceo")
        self.assertIsNone(d["written_to_crm_at"])
        self.assertIsNone(d["sent_at"])

    def test_an_ok_contact_gives_a_ready_draft_and_a_risky_one_a_risky_draft(self):
        self.verified(1, decision="OK")
        self.verified(2, decision="RISKY")
        self.run_stage()
        self.assertEqual(self.draft_row(1)["draft_status"], "ready")
        self.assertEqual(self.draft_row(2)["draft_status"], "risky")

    def test_the_band_comes_from_the_company_and_picks_the_template(self):
        for i, band in enumerate("ABCD", start=1):
            self.verified(i, band=band)
        self.run_stage()
        versions = {self.draft_row(i)["band"]: self.draft_row(i)["template_version"] for i in range(1, 5)}
        self.assertEqual({b: v[0] for b, v in versions.items()}, {b: b for b in "ABCD"})

    def test_the_company_moves_to_drafted_with_a_reason(self):
        self.verified(1)
        self.run_stage()
        st, reason = self.status_of(1)
        self.assertEqual(st, "drafted")
        self.assertTrue(reason)

    def test_the_first_name_is_inserted_by_code_after_generation(self):
        self.verified(1)
        seen = []
        self.run_stage(generator=lambda p: seen.append(json.dumps(p)) or (GOOD, {"input_tokens": 1, "output_tokens": 1}))
        self.assertNotIn(FIRST_NAME, seen[0])
        self.assertIn(FIRST_NAME, self.draft_row(1)["body_html"])

    def test_the_model_payload_never_holds_a_name_email_or_the_raw_title(self):
        self.verified(1)
        seen = []
        self.run_stage(generator=lambda p: seen.append(json.dumps(p)) or (GOOD, {"input_tokens": 1, "output_tokens": 1}))
        blob = seen[0]
        for needle in (FIRST_NAME, "quimby", "@", "Chief Executive Officer"):
            self.assertNotIn(needle, blob)
        self.assertEqual(json.loads(blob)["contact_function"], "ceo")

    def test_a_contact_with_no_function_still_gets_a_draft(self):
        self.verified(1, title_function=None)
        seen = []
        self.run_stage(generator=lambda p: seen.append(p) or (GOOD, {"input_tokens": 1, "output_tokens": 1}))
        self.assertIsNone(seen[0]["contact_function"])
        self.assertEqual(self.company_status(1), "drafted")


class SlotSources(DraftCase):
    BAD = {"opening_line": "Acme makes things.", "finding_sentence": "Eleven cookies fire on 11 pages, which is a violation."}

    def test_a_clean_first_answer_is_source_model(self):
        self.verified(1)
        self.run_stage()
        self.assertEqual(self.draft_row(1)["slot_source"], {"opening_line": "model", "finding_sentence": "model"})

    def test_a_bad_first_answer_is_regenerated_once(self):
        self.verified(1)
        replies = iter([(self.BAD, {"input_tokens": 400, "output_tokens": 40}), (GOOD, {"input_tokens": 410, "output_tokens": 45})])
        counts, _ = self.run_stage(generator=lambda p: next(replies))
        d = self.draft_row(1)
        self.assertEqual(d["slot_source"], {"opening_line": "retry", "finding_sentence": "retry"})
        self.assertEqual((d["attempts"], d["input_tokens"], d["output_tokens"]), (2, 810, 85))
        self.assertIn("finding_sentence", d["validation_log"][0])
        self.assertEqual(counts["slot_sources"]["retry"], 2)

    def test_two_bad_answers_use_the_fallback_slots(self):
        self.verified(1)
        calls = []
        counts, _ = self.run_stage(generator=lambda p: calls.append(1) or (self.BAD, {"input_tokens": 400, "output_tokens": 40}))
        d = self.draft_row(1)
        self.assertEqual(len(calls), 2, "regenerate once, not more")
        self.assertEqual(d["slot_source"], {"opening_line": "fallback", "finding_sentence": "fallback"})
        self.assertEqual(self.company_status(1), "drafted")
        f = d["findings"]
        self.assertEqual(draft_validate.validate({"opening_line": d["opening_line"], "finding_sentence": d["finding_sentence"]}, f), {})
        self.assertEqual(len(d["validation_log"]), 2)

    def test_one_good_slot_and_one_bad_slot_mix_sources(self):
        self.verified(1)
        replies = iter([({"opening_line": GOOD["opening_line"], "finding_sentence": "Fines apply."}, {"input_tokens": 1, "output_tokens": 1}),
                        ({"opening_line": "short", "finding_sentence": GOOD["finding_sentence"]}, {"input_tokens": 1, "output_tokens": 1})])
        self.run_stage(generator=lambda p: next(replies))
        self.assertEqual(self.draft_row(1)["slot_source"], {"opening_line": "model", "finding_sentence": "retry"})

    def test_unparseable_output_counts_as_a_failed_attempt(self):
        self.verified(1)
        replies = iter([(None, {"input_tokens": 5, "output_tokens": 5}), (GOOD, {"input_tokens": 5, "output_tokens": 5})])
        self.run_stage(generator=lambda p: next(replies))
        self.assertEqual(self.draft_row(1)["slot_source"]["opening_line"], "retry")

    def test_an_unavailable_model_leaves_the_company_verified_and_stops_the_stage(self):
        self.verified(1)
        self.verified(2)
        calls = []

        def down(payload):
            calls.append(1)
            raise draft_model.ModelUnavailable("connection refused")
        counts, _ = self.run_stage(generator=down)
        self.assertEqual((self.company_status(1), self.company_status(2)), ("verified", "verified"))
        self.assertEqual(len(calls), 1, "one failure stops the stage; a down server is not hammered")
        self.assertEqual(counts["stopped"], "model_unavailable")
        self.assertEqual(self.count("ae_drafts"), 0)

    def test_a_missing_key_stops_cleanly(self):
        self.verified(1)

        def nokey(payload):
            raise draft_model.ModelUnavailable("OPENAI_API_KEY is not set")
        counts, _ = self.run_stage(generator=nokey)
        self.assertEqual(self.company_status(1), "verified")
        self.assertIn("OPENAI_API_KEY", counts["error"])


class TestCohortAndCrm(DraftCase):
    def test_a_drafted_test_company_never_writes_to_the_crm(self):
        self.verified(1, is_test=True)
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            drafting.run(generator=lambda p: (GOOD, {"input_tokens": 1, "output_tokens": 1}), only_test=True)
        self.assertEqual(conn.sent, [])
        self.assertEqual(self.company_status(1), "drafted")

    def test_even_a_production_draft_writes_nothing_to_the_crm_in_this_milestone(self):
        self.verified(1)
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            drafting.run(generator=lambda p: (GOOD, {"input_tokens": 1, "output_tokens": 1}))
        self.assertEqual(conn.sent, [])

    def test_the_stage_module_has_no_crm_code_at_all(self):
        for name in ("drafting", "draft_model", "draft_validate", "draft_templates", "draft_findings", "draft_review"):
            src = (ROOT / "campaign" / f"{name}.py").read_text()
            self.assertNotIn("crm_write", src, name)
            self.assertNotIn("crm_read", src, name)
            self.assertNotIn("pymssql", src, name)

    def test_only_test_and_only_production_filters(self):
        self.verified(1, is_test=True)
        self.verified(2, is_test=False)
        self.run_stage(only_test=True)
        self.assertEqual((self.company_status(1), self.company_status(2)), ("drafted", "verified"))

    def test_the_draft_stage_is_wired_into_the_stage_table(self):
        from campaign import stages
        draft = next(s for s in stages.STAGES if s.name == "draft")
        self.assertTrue(draft.implemented)
        self.assertEqual(draft.number, 9)
        for src, dst in draft.transitions:
            self.assertTrue(status.is_allowed(src, dst), f"{src} -> {dst}")


class ReviewFile(DraftCase):
    def test_the_review_shows_each_draft_as_the_recipient_sees_it_with_its_provenance(self):
        self.verified(1, is_test=True)
        self.verified(2, is_test=True, decision="RISKY", band="A")
        self.run_stage(only_test=True)
        out = ROOT / "output" / "draft_review_test.html"
        try:
            path = draft_review.write_review(out, only_test=True)
            html = path.read_text()
        finally:
            out.unlink(missing_ok=True)
        for needle in ("Company 1", "Company 2", GOOD["opening_line"], GOOD["finding_sentence"], FIRST_NAME,
                       REPORT_URL, "placeholder", "model", "risky"):
            self.assertIn(needle, html, needle)
        self.assertIn("tracking_cookie_count", html, "the findings behind the draft are shown")
        self.assertIn("score", html.lower())

    def test_the_review_marks_fallback_slots(self):
        self.verified(1, is_test=True)
        self.run_stage(generator=lambda p: ({"opening_line": "x", "finding_sentence": "y"}, {"input_tokens": 1, "output_tokens": 1}),
                       only_test=True)
        out = ROOT / "output" / "draft_review_test2.html"
        try:
            html = draft_review.write_review(out, only_test=True).read_text()
        finally:
            out.unlink(missing_ok=True)
        self.assertIn("fallback", html)

    def test_the_review_escapes_what_it_shows(self):
        self.verified(1, is_test=True, first_name="<script>alert(1)</script>")
        self.run_stage(only_test=True)
        out = ROOT / "output" / "draft_review_test3.html"
        try:
            html = draft_review.write_review(out, only_test=True).read_text()
        finally:
            out.unlink(missing_ok=True)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_the_default_path_is_output_draft_review_html(self):
        self.assertEqual(draft_review.DEFAULT_PATH, ROOT / "output" / "draft_review.html")


if __name__ == "__main__":
    unittest.main()
