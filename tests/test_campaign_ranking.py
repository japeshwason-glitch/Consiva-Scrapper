"""
Auto Email Campaign, milestone 2, stage 7: contact ranking.

    SP=/tmp/claude-0/-root-japesh-Consiva-Scrapper/ef97ba72-bf6a-4433-ab7e-e932b9c6c55a/scratchpad
    $SP/run.sh api python -m unittest tests.test_campaign_ranking -v

Rules classify the common titles; the rest go to the llama-server (here, a fake one on a
local port: these tests never touch the real server). Ranking is by relevance, then the
company-size priority list, then seniority; a company whose best contact is not relevant
enough is held as held_no_contact; a server that cannot be reached leaves the company at
`reported` to be retried, never counted as "no contact".
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import ranking, settings_titles as st, title_llm, titles
from tests.test_campaign import DbCase


# --- a fake llama-server ---------------------------------------------------------------
class FakeLlama:
    """Answers /v1/chat/completions. `script` is a list of replies used first-in-first-out
    (("content", text) | ("status", code) | ("raw", body_text)); after it runs out `answers`
    maps the user message to (f, s, r)."""

    def __init__(self, answers=None, script=None):
        self.answers, self.script, self.requests = dict(answers or {}), list(script or []), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"body": body, "headers": dict(self.headers), "path": self.path})
                title = body["messages"][-1]["content"]
                kind, value = outer.script.pop(0) if outer.script else ("answer", title)
                if kind == "sleep":                       # answer, but too late for the client
                    time.sleep(value)
                    kind, value = "answer", title
                if kind == "status":
                    self.send_response(value)
                    self.end_headers()
                    return
                if kind == "raw":
                    payload = value
                else:
                    if kind == "answer":
                        f, s, r = outer.answers.get(value, ("other", 2, 1))
                        value = json.dumps({"f": f, "s": s, "r": r}, separators=(",", ":"))
                    payload = json.dumps({"choices": [{"message": {"content": value}}]})
                data = payload.encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:                           # the client gave up (timeout test)
                    pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def titles_asked(self):
        return [r["body"]["messages"][-1]["content"] for r in self.requests]


def dead_url():
    """A local address nothing listens on."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# --- pure: normalisation and rules ---------------------------------------------------------
class Normalise(unittest.TestCase):
    def test_case_punctuation_and_spacing(self):
        self.assertEqual(titles.normalize_title("  Sr. Manager - IT  "), "sr manager it")
        self.assertEqual(titles.normalize_title("Co-Founder & CEO"), "co founder and ceo")
        self.assertEqual(titles.normalize_title("Head\tof   IT/Infra"), "head of it infra")

    def test_empty_is_empty(self):
        for blank in (None, "", "   ", "-", "--- "):
            self.assertEqual(titles.normalize_title(blank), "", repr(blank))

    def test_two_spellings_share_one_key(self):
        self.assertEqual(titles.normalize_title("Head of IT"), titles.normalize_title("HEAD OF I.T."))


class Rules(unittest.TestCase):
    # title -> (function, seniority, relevance)
    CASES = {
        "CEO": ("ceo", 6, 5), "Chief Executive Officer": ("ceo", 6, 5),
        "Managing Director": ("ceo", 6, 5), "MD": ("ceo", 6, 5),
        "Founder": ("founder", 6, 5), "Co-Founder": ("founder", 6, 5),
        "Founder & CEO": ("founder", 6, 5), "Co-Founder & CTO": ("founder", 6, 5),
        "Proprietor": ("founder", 6, 5), "Partner": ("founder", 6, 5),
        "Managing Partner": ("founder", 6, 5), "Owner": ("founder", 6, 5),
        "CTO": ("tech", 6, 5), "CIO": ("tech", 6, 5),
        "Head of IT": ("tech", 5, 5), "IT Head": ("tech", 5, 5),
        "Vice President - Technology": ("tech", 5, 5), "GM - IT": ("tech", 5, 5),
        "Director - IT": ("tech", 5, 5), "Manager - IT": ("tech", 3, 3),
        "IT Executive": ("tech", 2, 2), "Software Engineer": ("tech", 2, 2),
        "CMO": ("marketing", 6, 5), "Head of Digital Marketing": ("marketing", 5, 5),
        "AVP - Marketing": ("marketing", 4, 4),
        "Sr. Manager - Digital Marketing": ("marketing", 3, 3),
        "Marketing Executive": ("marketing", 2, 2), "Intern - Marketing": ("marketing", 1, 1),
        "Senior Vice President - Marketing": ("marketing", 5, 5),
        "Data Protection Officer": ("dpo", 5, 5), "DPO": ("dpo", 5, 5),
        "CISO": ("security", 6, 5), "Head of Information Security": ("security", 5, 5),
        "Head of Legal": ("legal", 5, 5), "General Counsel": ("legal", 5, 5),
        "Company Secretary": ("legal", 5, 5), "Compliance Officer": ("legal", 4, 4),
        "Legal Manager": ("legal", 3, 3), "Chief Compliance Officer": ("legal", 6, 5),
        "Director": ("director", 5, 4), "Executive Director": ("director", 5, 4),
        "General Manager": ("director", 5, 4), "AVP": ("director", 4, 3),
        "Vice President": ("director", 5, 4),
        "Director - Operations": ("operations", 5, 3), "COO": ("operations", 6, 3),
        "CFO": ("finance", 6, 2), "HR Manager": ("hr", 3, 0), "HR Intern": ("hr", 1, 0),
        "Sales Director": ("sales", 5, 1), "VP Sales": ("sales", 5, 1),
        "Executive - Sales": ("sales", 2, 0), "Business Development Manager": ("sales", 3, 0),
        "Channel Partner Manager": ("sales", 3, 0),
    }

    def test_each_title(self):
        wrong = {}
        for title, want in self.CASES.items():
            got = titles.classify_by_rules(title)
            have = None if got is None else (got["function"], got["seniority"], got["relevance"])
            if have != want:
                wrong[title] = (have, want)
        self.assertEqual(wrong, {})

    def test_titles_the_rules_must_not_claim(self):
        for t in ("Product Manager", "Product Owner", "Business Analyst", "Strategy Consultant",
                  "Customer Support Lead", "Executive", "Associate", "Consultant"):
            self.assertIsNone(titles.classify_by_rules(t), t)

    def test_avp_ranks_below_vp(self):
        avp = titles.classify_by_rules("AVP - Marketing")["seniority"]
        vp = titles.classify_by_rules("VP - Marketing")["seniority"]
        self.assertLess(avp, vp)

    def test_executive_is_junior_but_executive_director_is_not(self):
        self.assertEqual(titles.classify_by_rules("Marketing Executive")["seniority"], 2)
        self.assertEqual(titles.classify_by_rules("Executive Director")["seniority"], 5)

    def test_gm_with_a_department_takes_the_department(self):
        self.assertEqual(titles.classify_by_rules("GM - Marketing")["function"], "marketing")
        self.assertEqual(titles.classify_by_rules("General Manager")["function"], "director")

    def test_the_result_names_the_rule(self):
        self.assertTrue(titles.classify_by_rules("CEO")["rule"])

    def test_empty_titles_match_nothing(self):
        self.assertIsNone(titles.classify_by_rules(None))
        self.assertIsNone(titles.classify_by_rules(""))

    def test_every_rule_function_is_in_the_enum_and_every_number_in_range(self):
        for title in self.CASES:
            c = titles.classify_by_rules(title)
            self.assertIn(c["function"], st.FUNCTIONS)
            self.assertIn(c["seniority"], range(1, 7))
            self.assertIn(c["relevance"], range(0, 6))


class SizeLists(unittest.TestCase):
    def test_scale_to_size(self):
        small = ("MSME", "Startup", "1-50", "51-100")
        mid = ("SMB", "101-250", "251-500")
        large = ("Enterprise", "501-1000", "1001-2500", "2501 -5000", "5001 & above")
        for s in small:
            self.assertEqual(st.size_class(s), "small", s)
        for s in mid:
            self.assertEqual(st.size_class(s), "mid", s)
        for s in large:
            self.assertEqual(st.size_class(s), "large", s)

    def test_unknown_or_missing_scale_is_blended(self):
        for s in (None, "", "  ", "Medium-ish"):
            self.assertEqual(st.size_class(s), "blended", repr(s))

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(st.size_class("  enterprise "), "large")
        self.assertEqual(st.size_class("5001&above"), "large")

    def test_the_spec_lists(self):
        self.assertEqual(st.PRIORITY_LISTS["small"], ("founder", "ceo", "director"))
        self.assertEqual(st.PRIORITY_LISTS["mid"], ("marketing", "tech"))
        self.assertEqual(st.PRIORITY_LISTS["large"], ("dpo", "legal", "security", "marketing"))
        self.assertTrue(set(st.PRIORITY_LISTS["blended"]) >= {"founder", "ceo", "dpo", "marketing", "tech"})

    def test_every_listed_function_exists(self):
        for lst in st.PRIORITY_LISTS.values():
            self.assertTrue(set(lst) <= set(st.FUNCTIONS))


# --- pure: the llm request ------------------------------------------------------------------
class LlmRequest(unittest.TestCase):
    """The gignati (llama-server) provider: unchanged by the provider switch."""

    def setUp(self):
        self.srv = FakeLlama(answers={"Strategy Consultant": ("other", 2, 1)})
        self.addCleanup(self.srv.stop)
        patch = mock.patch.object(st, "TITLE_PROVIDER", "gignati")
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_body_is_what_the_spec_says(self):
        out = title_llm.classify_title("Strategy Consultant", base_url=self.srv.url)
        self.assertEqual(out, {"f": "other", "s": 2, "r": 1})
        body = self.srv.requests[0]["body"]
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 40)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        rf = body["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertTrue(rf["json_schema"]["strict"])
        props = rf["json_schema"]["schema"]["properties"]
        self.assertEqual(set(props), {"f", "s", "r"})
        self.assertEqual(props["f"]["enum"], list(st.FUNCTIONS))
        self.assertEqual(rf["json_schema"]["schema"]["required"], ["f", "s", "r"])
        self.assertFalse(rf["json_schema"]["schema"]["additionalProperties"])

    def test_the_title_is_only_in_the_user_message(self):
        title_llm.classify_title("Strategy Consultant", base_url=self.srv.url)
        msgs = self.srv.requests[0]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertEqual(msgs[1]["content"], "Strategy Consultant")
        self.assertNotIn("Strategy Consultant", msgs[0]["content"])

    def test_the_system_prompt_is_byte_identical_across_calls(self):
        for t in ("Strategy Consultant", "Product Manager", "Business Analyst", "Customer Support Lead"):
            title_llm.classify_title(t, base_url=self.srv.url)
        systems = {r["body"]["messages"][0]["content"] for r in self.srv.requests}
        self.assertEqual(len(self.srv.requests), 4)
        self.assertEqual(systems, {title_llm.SYSTEM_PROMPT})

    def test_the_prompt_teaches_the_indian_title_conventions(self):
        p = title_llm.SYSTEM_PROMPT
        for needle in ("Executive", "AVP", "GM", "Proprietor", "Partner", "Director - Operations",
                       "Sr. Manager", "Manager - IT", "compact"):
            self.assertIn(needle, p, needle)

    def test_it_sends_browser_like_headers(self):
        title_llm.classify_title("Strategy Consultant", base_url=self.srv.url)
        self.assertIn("Mozilla", self.srv.requests[0]["headers"]["User-Agent"])
        self.assertEqual(self.srv.requests[0]["headers"]["Content-Type"], "application/json")

    def test_invalid_output_is_retried_once_then_gives_up(self):
        bad = ('content', '{"f":"astrologer","s":9,"r":7}')
        self.srv.script = [bad, bad]
        self.assertIsNone(title_llm.classify_title("Strategy Consultant", base_url=self.srv.url))
        self.assertEqual(len(self.srv.requests), 2)

    def test_every_kind_of_invalid_output_is_rejected(self):
        for text in ("not json at all", '{"f":"tech","s":3}', '{"f":"tech","s":0,"r":3}',
                     '{"f":"tech","s":3,"r":6}', '{"f":"tech","s":"3","r":3}',
                     '{"f":"tech","s":true,"r":3}', '{"f":"tech","s":3,"r":3,"x":1}',
                     '["tech",3,3]', '', '{"f":"Tech","s":3,"r":3}'):
            self.srv.script = [("content", text), ("content", text)]
            self.assertIsNone(title_llm.classify_title("X", base_url=self.srv.url), repr(text))

    def test_a_valid_second_try_is_used(self):
        self.srv.script = [("content", "garbage"), ("content", '{"f":"tech","s":3,"r":4}')]
        self.assertEqual(title_llm.classify_title("Strategy Consultant", base_url=self.srv.url),
                         {"f": "tech", "s": 3, "r": 4})
        self.assertEqual(len(self.srv.requests), 2)

    def test_pretty_printed_json_is_still_accepted(self):
        self.srv.script = [("content", '{\n  "f": "tech",\n  "s": 3,\n  "r": 4\n}')]
        self.assertEqual(title_llm.classify_title("X", base_url=self.srv.url), {"f": "tech", "s": 3, "r": 4})

    def test_an_unreachable_server_raises_and_is_not_retried_as_invalid(self):
        with self.assertRaises(title_llm.ServerUnavailable):
            title_llm.classify_title("X", base_url=dead_url(), timeout=2)

    def test_a_server_error_raises(self):
        for code in (500, 502, 503, 403, 429):
            self.srv.script = [("status", code)]
            with self.assertRaises(title_llm.ServerUnavailable, msg=str(code)):
                title_llm.classify_title("X", base_url=self.srv.url)

    def test_a_non_json_http_body_is_a_server_problem_not_an_invalid_title(self):
        self.srv.script = [("raw", "<html>Just a moment...</html>")]
        with self.assertRaises(title_llm.ServerUnavailable):
            title_llm.classify_title("X", base_url=self.srv.url)


# --- the stage ------------------------------------------------------------------------------
class RankCase(DbCase):
    def setUp(self):
        super().setUp()
        self.srv = FakeLlama()
        self.addCleanup(self.srv.stop)
        for name, value in (("LLM_BASE_URL", self.srv.url), ("TITLE_PROVIDER", "gignati")):
            patch = mock.patch.object(st, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def reported(self, company_id, scale=None, is_test=False):
        self.add_company(company_id, "reported", is_test=is_test)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET company_scale = %s WHERE company_id = %s", (scale, company_id))

    def contact(self, company_id, title, passed=True, check_reason=None):
        cid = str(uuid.uuid4())
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, job_title, check_passed, check_reason)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (cid, company_id, f"{cid[:8]}@c{company_id}.example.com", title, passed, check_reason))
        return cid

    def rows(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("""SELECT * FROM ae_contacts WHERE company_id = %s ORDER BY id""", (company_id,))
            return [dict(r) for r in cur.fetchall()]

    def by_title(self, company_id):
        return {r["job_title"]: r for r in self.rows(company_id)}

    def order(self, company_id):
        return [r["job_title"] for r in sorted((r for r in self.rows(company_id) if r["rank"]),
                                               key=lambda r: r["rank"])]

    def put_cache(self, norm, function, seniority, relevance, version=None, provider=None, model=None,
                  source="llm"):
        """An llm cache row for the active provider and model unless told otherwise."""
        provider = provider if provider is not None else st.TITLE_PROVIDER
        model = model if model is not None else st.active_provider()[1]
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_title_cache (normalized_title, function, seniority, relevance, source,
                                                       prompt_version, provider, model)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        (norm, function, str(seniority), str(relevance), source,
                         version or st.PROMPT_VERSION, provider, model))

    def cache_rows(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_title_cache ORDER BY normalized_title")
            return [dict(r) for r in cur.fetchall()]


class Priority(RankCase):
    def test_small_company_prefers_founder_ceo_director(self):
        self.reported(1, "MSME")
        for t in ("Head of Marketing", "CEO", "Director - IT", "HR Manager"):
            self.contact(1, t)
        ranking.run()
        self.assertEqual(self.order(1)[0], "CEO")
        self.assertEqual(self.order(1)[-1], "HR Manager")

    def test_small_company_founder_beats_ceo(self):
        self.reported(1, "Startup")
        for t in ("CEO", "Founder"):
            self.contact(1, t)
        ranking.run()
        self.assertEqual(self.order(1), ["Founder", "CEO"])

    def test_mid_company_marketing_then_tech(self):
        self.reported(1, "SMB")
        for t in ("CEO", "CTO", "Head of Marketing"):
            self.contact(1, t)
        ranking.run()
        self.assertEqual(self.order(1), ["Head of Marketing", "CTO", "CEO"])

    def test_large_company_dpo_legal_security_marketing(self):
        self.reported(1, "Enterprise")
        for t in ("CTO", "Head of Marketing", "CISO", "General Counsel", "Data Protection Officer"):
            self.contact(1, t)
        ranking.run()
        self.assertEqual(self.order(1), ["Data Protection Officer", "General Counsel", "CISO",
                                         "Head of Marketing", "CTO"])

    def test_unknown_size_uses_the_blended_list(self):
        self.reported(1, None)
        for t in ("CTO", "Data Protection Officer", "CEO"):
            self.contact(1, t)
        ranking.run()
        self.assertEqual(self.order(1), ["CEO", "Data Protection Officer", "CTO"])

    def test_relevance_comes_before_the_priority_list(self):
        self.reported(1, "MSME")
        self.contact(1, "Director - Operations")       # on the small list as 'director'? no: operations, relevance 3
        self.contact(1, "Marketing Executive")         # relevance 2
        self.contact(1, "CTO")                         # relevance 5
        ranking.run()
        self.assertEqual(self.order(1), ["CTO", "Director - Operations", "Marketing Executive"])

    def test_seniority_breaks_a_tie_after_the_list(self):
        self.reported(1, "Enterprise")                 # neither title is on the large list's functions
        self.contact(1, "IT Manager")                  # tech, seniority 3 -> relevance 3
        self.contact(1, "Head of IT")                  # tech, seniority 5 -> relevance 5
        ranking.run()
        self.assertEqual(self.order(1), ["Head of IT", "IT Manager"])

    def test_same_function_and_relevance_falls_to_seniority_not_to_row_order(self):
        self.reported(1, "SMB")
        self.contact(1, "AVP - Marketing")             # marketing 4 -> relevance 4
        self.contact(1, "VP - Marketing")              # marketing 5 -> relevance 5
        ranking.run()
        self.assertEqual(self.order(1), ["VP - Marketing", "AVP - Marketing"])

    def test_the_scale_is_stored_and_used_not_guessed_from_the_title(self):
        self.reported(1, "Enterprise")
        self.reported(2, "MSME")
        for cid in (1, 2):
            self.contact(cid, "Head of Marketing")
            self.contact(cid, "CEO")
        ranking.run()
        self.assertEqual(self.order(1)[0], "Head of Marketing")      # large: marketing is on the list, ceo is not
        self.assertEqual(self.order(2)[0], "CEO")                    # small: ceo on the list


class Outcomes(RankCase):
    def test_a_good_contact_means_contact_ranked(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")
        counts = ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")
        self.assertEqual(counts["contact_ranked"], 1)

    def test_only_hr_sales_and_interns_means_held_no_contact(self):
        self.reported(1, "SMB")
        for t in ("HR Manager", "Sales Executive", "Marketing Intern"):
            self.contact(1, t)
        counts = ranking.run()
        status, reason = self.status_of(1)
        self.assertEqual(status, "held_no_contact")
        self.assertIn("relevance", reason)
        self.assertEqual(counts["held_no_contact"], 1)

    def test_the_threshold_is_in_config_and_is_inclusive(self):
        self.reported(1, "SMB")
        self.contact(1, "IT Executive")                 # relevance 2
        self.assertEqual(st.RELEVANCE_MIN, 2)
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")
        self.reported(2, "SMB")
        self.contact(2, "HR Manager")                   # relevance 0
        ranking.run()
        self.assertEqual(self.status_of(2)[0], "held_no_contact")

    def test_ranks_start_at_one_and_every_contact_has_a_rank_and_a_reason(self):
        self.reported(1, "SMB")
        for t in ("CTO", "HR Manager", "Head of Marketing", "Intern - Sales"):
            self.contact(1, t)
        ranking.run()
        rows = self.rows(1)
        self.assertEqual(sorted(r["rank"] for r in rows), [1, 2, 3, 4])
        for r in rows:
            self.assertTrue(r["rank_reason"], r["job_title"])
            self.assertIn(r["title_source"], ("rule", "llm"))
        top = [r for r in rows if r["rank"] == 1][0]
        self.assertIn("rule", top["rank_reason"])
        self.assertIn("marketing", top["rank_reason"])

    def test_the_reason_names_function_seniority_relevance_and_the_size_list(self):
        self.reported(1, "Enterprise")
        self.contact(1, "Data Protection Officer")
        ranking.run()
        reason = self.rows(1)[0]["rank_reason"]
        for needle in ("dpo", "seniority 5", "relevance 5", "large", "rule"):
            self.assertIn(needle, reason)

    def test_classification_is_stored_on_the_contact(self):
        self.reported(1, "SMB")
        self.contact(1, "Head of IT")
        ranking.run()
        r = self.rows(1)[0]
        self.assertEqual((r["title_function"], r["title_seniority"], r["title_relevance"], r["title_source"]),
                         ("tech", 5, 5, "rule"))

    def test_contacts_that_failed_the_free_checks_are_not_ranked(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")
        self.contact(1, "CEO", passed=False, check_reason="role_address")
        self.contact(1, "CFO", passed=None)
        ranking.run()
        t = self.by_title(1)
        self.assertEqual(t["CTO"]["rank"], 1)
        self.assertIsNone(t["CEO"]["rank"])
        self.assertIn("role_address", t["CEO"]["rank_reason"])
        self.assertIsNone(t["CFO"]["rank"])

    def test_a_company_with_no_passing_contact_is_held_not_ranked(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO", passed=False, check_reason="no_mx")
        ranking.run()
        status, reason = self.status_of(1)
        self.assertEqual(status, "held_no_contact")
        self.assertIn("free check", reason)

    def test_only_reported_companies_are_touched(self):
        for i, s in enumerate(("pending", "checked", "scanned", "contact_ranked", "legacy"), start=10):
            self.add_company(i, s)
            self.contact(i, "CTO")
        self.assertEqual(ranking.run()["companies_claimed"], 0)
        for i in range(10, 15):
            self.assertIsNone(self.rows(i)[0]["rank"])

    def test_limit_caps_the_companies(self):
        for i in range(1, 6):
            self.reported(i, "SMB")
            self.contact(i, "CTO")
        ranking.run(limit=2)
        self.assertEqual(self.count("ae_companies", "status = 'contact_ranked'"), 2)
        self.assertEqual(self.count("ae_companies", "status = 'reported'"), 3)

    def test_only_test_filters_the_cohort(self):
        self.reported(1, "SMB", is_test=True)
        self.reported(2, "SMB", is_test=False)
        for i in (1, 2):
            self.contact(i, "CTO")
        ranking.run(only_test=True)
        self.assertEqual(self.status_of(1)[0], "contact_ranked")
        self.assertEqual(self.status_of(2)[0], "reported")

    def test_a_second_run_changes_nothing(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")
        ranking.run()
        before = self.rows(1)
        self.assertEqual(ranking.run()["companies_claimed"], 0)
        self.assertEqual(self.rows(1), before)

    def test_rule_titles_never_call_the_server(self):
        self.reported(1, "SMB")
        for t in ("CTO", "CEO", "Head of Marketing"):
            self.contact(1, t)
        counts = ranking.run()
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(counts["llm_calls"], 0)
        self.assertEqual(counts["titles_rule"], 3)


class NoTitle(RankCase):
    def test_a_contact_with_no_title_ranks_last_with_source_none(self):
        self.reported(1, "SMB")
        self.contact(1, None)
        self.contact(1, "   ")
        self.contact(1, "HR Manager")
        self.contact(1, "CTO")
        ranking.run()
        rows = self.rows(1)
        notitle = [r for r in rows if not (r["job_title"] or "").strip()]
        self.assertEqual({r["title_source"] for r in notitle}, {"none"})
        self.assertEqual(sorted(r["rank"] for r in notitle), [3, 4])
        self.assertIn("no job title", notitle[0]["rank_reason"])
        self.assertEqual(self.by_title(1)["CTO"]["rank"], 1)

    def test_a_company_whose_only_contact_has_no_title_is_held(self):
        self.reported(1, "SMB")
        self.contact(1, None)
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "held_no_contact")
        self.assertEqual(self.srv.requests, [])


class Llm(RankCase):
    def test_a_leftover_title_is_classified_by_the_model_and_stored(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        counts = ranking.run()
        r = self.rows(1)[0]
        self.assertEqual((r["title_function"], r["title_seniority"], r["title_relevance"], r["title_source"]),
                         ("tech", 5, 4, "llm"))
        self.assertIn("llm", r["rank_reason"])
        self.assertEqual((counts["llm_calls"], counts["titles_llm"]), (1, 1))
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_the_result_is_cached_by_normalised_title_with_source_and_version(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        (row,) = [r for r in self.cache_rows() if r["source"] == "llm"]
        self.assertEqual((row["normalized_title"], row["function"], int(row["seniority"]), int(row["relevance"]),
                          row["prompt_version"]), ("strategy consultant", "tech", 5, 4, st.PROMPT_VERSION))

    def test_a_cache_hit_makes_no_call(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.reported(2, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(2, "STRATEGY  consultant.")        # same normalised title
        ranking.run()
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.rows(2)[0]["title_source"], "llm")
        self.assertEqual(self.status_of(2)[0], "contact_ranked")

    def test_a_cache_hit_counts_separately(self):
        self.reported(1, "SMB")
        self.reported(2, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(2, "Strategy Consultant")
        counts = ranking.run()
        self.assertEqual((counts["llm_calls"], counts["cache_hits"], counts["titles_llm"]), (1, 1, 2))

    def test_a_cached_row_from_another_prompt_version_is_not_reused(self):
        self.put_cache("strategy consultant", "hr", 1, 0, version="old-version")
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.rows(1)[0]["title_function"], "tech")
        (row,) = self.cache_rows()
        self.assertEqual((row["function"], row["prompt_version"]), ("tech", st.PROMPT_VERSION))

    def test_a_current_cache_row_is_used_as_is(self):
        self.put_cache("strategy consultant", "legal", 3, 3)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(self.rows(1)[0]["title_function"], "legal")

    def test_rule_matches_are_cached_too_as_source_rule(self):
        self.reported(1, "SMB")
        self.contact(1, "Head of IT")
        ranking.run()
        (row,) = self.cache_rows()
        self.assertEqual((row["normalized_title"], row["source"], row["function"]), ("head of it", "rule", "tech"))

    def test_invalid_output_is_retried_once_then_the_title_is_unclassified_and_ranks_last(self):
        bad = ("content", '{"f":"astrologer","s":9,"r":7}')
        self.srv.script = [bad, bad]
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(1, "CTO")
        counts = ranking.run()
        t = self.by_title(1)
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(t["Strategy Consultant"]["title_source"], "unclassified")
        self.assertEqual(t["Strategy Consultant"]["rank"], 2)
        self.assertIn("unclassified", t["Strategy Consultant"]["rank_reason"])
        self.assertEqual(t["CTO"]["rank"], 1)
        self.assertEqual(counts["titles_unclassified"], 1)
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_an_unclassified_title_is_not_cached(self):
        bad = ("content", "nonsense")
        self.srv.script = [bad, bad]
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(1, "CTO")
        ranking.run()
        self.assertEqual([r for r in self.cache_rows() if r["source"] == "llm"], [])

    def test_unclassified_ranks_below_even_a_zero_relevance_title_and_above_no_title(self):
        bad = ("content", "nonsense")
        self.srv.script = [bad, bad]
        self.reported(1, "SMB")
        self.contact(1, None)
        self.contact(1, "Strategy Consultant")
        self.contact(1, "HR Manager")
        self.contact(1, "CTO")
        ranking.run()
        self.assertEqual(self.order(1), ["CTO", "HR Manager", "Strategy Consultant", None])

    def test_a_company_with_only_an_unclassified_contact_is_held(self):
        bad = ("content", "nonsense")
        self.srv.script = [bad, bad]
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        status, reason = self.status_of(1)
        self.assertEqual(status, "held_no_contact")
        self.assertIn("unclassified", reason)


class ServerDown(RankCase):
    def setUp(self):
        super().setUp()
        st.LLM_BASE_URL = dead_url()

    def test_a_down_server_leaves_the_company_at_reported_with_nothing_written(self):
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(1, "CTO")
        counts = ranking.run()
        self.assertEqual(self.status_of(1)[0], "reported")
        for r in self.rows(1):
            self.assertIsNone(r["rank"])
            self.assertIsNone(r["rank_reason"])
            self.assertIsNone(r["title_source"])
        self.assertEqual(counts["deferred_llm_unavailable"], 1)
        self.assertEqual(counts["held_no_contact"], 0)

    def test_it_is_never_counted_as_no_contact(self):
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertNotEqual(self.status_of(1)[0], "held_no_contact")

    def test_the_company_is_retried_next_run_and_succeeds_once_the_server_is_back(self):
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "reported")
        st.LLM_BASE_URL = self.srv.url
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_a_server_error_status_is_treated_the_same(self):
        st.LLM_BASE_URL = self.srv.url
        self.srv.script = [("status", 503)]
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "reported")

    def test_companies_that_need_no_model_still_complete_while_it_is_down(self):
        self.reported(1, "SMB")
        self.reported(2, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(2, "CTO")
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "reported")
        self.assertEqual(self.status_of(2)[0], "contact_ranked")

    def test_after_the_first_failure_the_run_stops_calling_the_server(self):
        st.LLM_BASE_URL = self.srv.url
        self.srv.script = [("status", 503), ("status", 503), ("status", 503)]
        for i in (1, 2, 3):
            self.reported(i, "SMB")
            self.contact(i, "Strategy Consultant")
        counts = ranking.run()
        self.assertEqual(len(self.srv.requests), 1, "do not pay a timeout per company")
        self.assertEqual(counts["deferred_llm_unavailable"], 3)

    def test_titles_already_cached_still_work_while_the_server_is_down(self):
        self.put_cache("strategy consultant", "tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_a_failure_part_way_does_not_leave_half_a_company_ranked(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")                         # rule
        self.contact(1, "Strategy Consultant")         # needs the server
        ranking.run()
        self.assertTrue(all(r["rank"] is None for r in self.rows(1)))


class DryRun(RankCase):
    def test_a_dry_run_writes_nothing_and_calls_nothing(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")
        self.contact(1, "Strategy Consultant")
        counts = ranking.run(dry_run=True)
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(self.status_of(1)[0], "reported")
        for r in self.rows(1):
            self.assertIsNone(r["rank"])
            self.assertIsNone(r["title_source"])
        self.assertEqual(self.cache_rows(), [])
        self.assertEqual(counts["would_need_llm"], ["Strategy Consultant"])
        self.assertEqual(counts["titles_rule"], 1)
        self.assertEqual(counts["llm_calls"], 0)

    def test_a_dry_run_reads_the_cache_so_it_does_not_overstate_the_work(self):
        self.put_cache("strategy consultant", "tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        counts = ranking.run(dry_run=True)
        self.assertEqual(counts["would_need_llm"], [])
        self.assertEqual(counts["cache_hits"], 1)

    def test_a_dry_run_reports_what_would_be_decided(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")
        self.reported(2, "SMB")
        self.contact(2, "HR Manager")
        counts = ranking.run(dry_run=True)
        self.assertEqual((counts["contact_ranked"], counts["held_no_contact"]), (1, 1))


# --- the provider switch --------------------------------------------------------------------------------
FAKE_KEY = "sk-test-NOT-A-REAL-KEY-0123456789"


class ProviderConfig(unittest.TestCase):
    def run_py(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("AE_") and k != "OPENAI_API_KEY"}
        code = ("from campaign import settings_titles as st; "
                "print(st.TITLE_PROVIDER, st.OPENAI_BASE_URL, st.OPENAI_MODEL, st.active_provider())")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT),
                              env={**base, **env}, timeout=60)

    def test_the_defaults_are_openai_gpt_4_1_nano(self):
        r = self.run_py()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[0:3], ["openai", "https://api.openai.com/v1", "gpt-4.1-nano"])

    def test_the_env_names_switch_provider_url_and_model(self):
        r = self.run_py(AE_TITLE_PROVIDER="gignati")
        self.assertEqual(r.stdout.split()[0], "gignati")
        r = self.run_py(AE_OPENAI_BASE_URL="http://x.test/v1", AE_OPENAI_TITLE_MODEL="gpt-x")
        self.assertIn("http://x.test/v1", r.stdout)
        self.assertIn("gpt-x", r.stdout)

    def test_active_provider_names_the_provider_and_model(self):
        with mock.patch.object(st, "TITLE_PROVIDER", "openai"), mock.patch.object(st, "OPENAI_MODEL", "m1"):
            self.assertEqual(st.active_provider(), ("openai", "m1"))
        with mock.patch.object(st, "TITLE_PROVIDER", "gignati"):
            self.assertEqual(st.active_provider(), ("gignati", st.GIGNATI_MODEL))

    def test_an_unknown_provider_is_an_error_not_a_silent_default(self):
        with mock.patch.object(st, "TITLE_PROVIDER", "claude"):
            with self.assertRaises(ValueError):
                st.active_provider()
            with self.assertRaises(ValueError):
                title_llm.classify_title("X")

    def test_the_key_is_never_a_setting(self):
        self.assertFalse([n for n in dir(st) if "KEY" in n.upper()], "the API key must only ever come from the environment")


class OpenAiRequest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeLlama(answers={"Strategy Consultant": ("other", 2, 1)})
        self.addCleanup(self.srv.stop)
        self.base = f"{self.srv.url}/openai/v1"
        for target, name, value in ((st, "TITLE_PROVIDER", "openai"), (st, "OPENAI_BASE_URL", self.base)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY})
        patch.start()
        self.addCleanup(patch.stop)

    def classify(self, title="Strategy Consultant", **kw):
        return title_llm.classify_title(title, **kw)

    def test_it_posts_to_chat_completions_under_the_configured_base(self):
        self.assertEqual(self.classify(), {"f": "other", "s": 2, "r": 1})
        self.assertEqual(self.srv.requests[0]["path"], "/openai/v1/chat/completions")

    def test_the_key_goes_only_in_the_bearer_header(self):
        self.classify()
        req = self.srv.requests[0]
        self.assertEqual(req["headers"]["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertNotIn(FAKE_KEY, json.dumps(req["body"]))
        self.assertNotIn(FAKE_KEY, req["path"])
        others = {k: v for k, v in req["headers"].items() if k.lower() != "authorization"}
        self.assertNotIn(FAKE_KEY, json.dumps(others))

    def test_the_body_is_strict_structured_output_with_enums_only(self):
        self.classify()
        body = self.srv.requests[0]["body"]
        self.assertEqual(body["model"], st.OPENAI_MODEL)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_completion_tokens"], 40)
        rf = body["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        js = rf["json_schema"]
        self.assertTrue(js["strict"])
        self.assertRegex(js["name"], r"^[A-Za-z0-9_-]+$")
        schema = js["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(sorted(schema["required"]), ["f", "r", "s"])
        self.assertEqual(set(schema["properties"]), {"f", "s", "r"})
        self.assertEqual(schema["properties"]["f"]["enum"], list(st.FUNCTIONS))
        self.assertEqual(schema["properties"]["s"]["enum"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(schema["properties"]["r"]["enum"], [0, 1, 2, 3, 4, 5])
        for prop in schema["properties"].values():
            self.assertNotIn("minimum", prop)
            self.assertNotIn("maximum", prop)

    def test_nothing_gignati_only_is_sent(self):
        self.classify()
        body = self.srv.requests[0]["body"]
        for key in ("chat_template_kwargs", "max_tokens"):
            self.assertNotIn(key, body)
        self.assertNotIn("enable_thinking", json.dumps(body))

    def test_the_system_prompt_is_the_same_constant_and_the_user_message_is_only_the_title(self):
        for t in ("Strategy Consultant", "Product Manager", "Business Analyst"):
            self.classify(t)
        for req in self.srv.requests:
            msgs = req["body"]["messages"]
            self.assertEqual([m["role"] for m in msgs], ["system", "user"])
            self.assertEqual(msgs[0]["content"], title_llm.SYSTEM_PROMPT)
        self.assertEqual([r["body"]["messages"][1]["content"] for r in self.srv.requests],
                         ["Strategy Consultant", "Product Manager", "Business Analyst"])

    def test_the_system_prompt_is_the_same_for_both_providers(self):
        self.classify()
        with mock.patch.object(st, "TITLE_PROVIDER", "gignati"):
            title_llm.classify_title("Strategy Consultant", base_url=self.srv.url)
        a, b = (r["body"]["messages"][0]["content"] for r in self.srv.requests)
        self.assertEqual(a, b)

    def test_the_gignati_provider_is_unchanged_next_to_it(self):
        with mock.patch.object(st, "TITLE_PROVIDER", "gignati"):
            title_llm.classify_title("Strategy Consultant", base_url=self.srv.url)
        req = self.srv.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["body"]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(req["body"]["max_tokens"], 40)
        self.assertNotIn("model", req["body"])
        self.assertNotIn("max_completion_tokens", req["body"])
        self.assertNotIn("Authorization", req["headers"])

    def test_invalid_output_is_retried_once_then_gives_up(self):
        bad = ("content", '{"f":"astrologer","s":9,"r":7}')
        self.srv.script = [bad, bad]
        self.assertIsNone(self.classify())
        self.assertEqual(len(self.srv.requests), 2)

    def test_a_valid_second_try_is_used(self):
        self.srv.script = [("content", "garbage"), ("content", '{"f":"tech","s":3,"r":4}')]
        self.assertEqual(self.classify(), {"f": "tech", "s": 3, "r": 4})
        self.assertEqual(len(self.srv.requests), 2)

    def test_a_refusal_with_no_content_is_invalid_output_not_an_outage(self):
        self.srv.script = [("raw", json.dumps({"choices": [{"message": {"content": None, "refusal": "no"}}]})),
                           ("raw", json.dumps({"choices": [{"message": {"content": None, "refusal": "no"}}]}))]
        self.assertIsNone(self.classify())

    def test_rate_limits_and_server_errors_are_unreachable(self):
        for code in (429, 500, 502, 503, 401, 403):
            self.srv.script = [("status", code)]
            with self.assertRaises(title_llm.ServerUnavailable, msg=str(code)):
                self.classify()
            self.assertEqual(len(self.srv.requests), 1, "an HTTP error is not retried as invalid output")
            self.srv.requests.clear()

    def test_a_timeout_is_unreachable(self):
        self.srv.script = [("sleep", 2)]
        with self.assertRaises(title_llm.ServerUnavailable):
            self.classify(timeout=0.5)

    def test_a_connection_refused_is_unreachable(self):
        with mock.patch.object(st, "OPENAI_BASE_URL", dead_url() + "/v1"):
            with self.assertRaises(title_llm.ServerUnavailable):
                self.classify(timeout=2)

    def test_the_key_never_appears_in_an_error_message(self):
        for script in ([("status", 401)], [("raw", "<html>no</html>")]):
            self.srv.script = list(script)
            with self.assertRaises(title_llm.ServerUnavailable) as ctx:
                self.classify()
            self.assertNotIn(FAKE_KEY, str(ctx.exception))
            self.assertNotIn(FAKE_KEY, repr(ctx.exception))

    def test_a_missing_key_stops_cleanly_before_any_request(self):
        for env in ({}, {"OPENAI_API_KEY": ""}, {"OPENAI_API_KEY": "   "}):
            with mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("OPENAI_API_KEY", None)
                with self.assertRaises(title_llm.ProviderNotConfigured):
                    self.classify()
        self.assertEqual(self.srv.requests, [])

    def test_a_missing_key_is_a_kind_of_server_unavailable(self):
        self.assertTrue(issubclass(title_llm.ProviderNotConfigured, title_llm.ServerUnavailable))

    def test_gignati_needs_no_key(self):
        with mock.patch.object(st, "TITLE_PROVIDER", "gignati"), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertEqual(title_llm.classify_title("Strategy Consultant", base_url=self.srv.url),
                             {"f": "other", "s": 2, "r": 1})


class OpenAiStage(RankCase):
    def setUp(self):
        super().setUp()
        for name, value in (("TITLE_PROVIDER", "openai"), ("OPENAI_BASE_URL", f"{self.srv.url}/openai/v1"),
                            ("OPENAI_MODEL", "gpt-4.1-nano")):
            patch = mock.patch.object(st, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_leftover_title_is_classified_and_the_stage_completes(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        counts = ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")
        self.assertEqual(self.srv.requests[0]["path"], "/openai/v1/chat/completions")
        self.assertEqual((counts["llm_calls"], counts["titles_llm"]), (1, 1))

    def test_rules_still_come_first_and_never_call_openai(self):
        self.reported(1, "SMB")
        for t in ("CTO", "CEO"):
            self.contact(1, t)
        ranking.run()
        self.assertEqual(self.srv.requests, [])

    def test_provider_model_and_version_are_stored_on_the_cache_row(self):
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        (row,) = [r for r in self.cache_rows() if r["source"] == "llm"]
        self.assertEqual((row["provider"], row["model"], row["prompt_version"]),
                         ("openai", "gpt-4.1-nano", st.PROMPT_VERSION))

    def test_rule_rows_stay_provider_less(self):
        self.reported(1, "SMB")
        self.contact(1, "Head of IT")
        ranking.run()
        (row,) = self.cache_rows()
        self.assertEqual((row["source"], row["provider"], row["model"]), ("rule", None, None))

    def test_the_counts_name_the_provider_and_model_used(self):
        self.reported(1, "SMB")
        self.contact(1, "CTO")
        counts = ranking.run()
        self.assertEqual((counts["provider"], counts["model"]), ("openai", "gpt-4.1-nano"))

    def test_the_rank_reason_names_the_provider_for_a_model_title(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertIn("openai", self.rows(1)[0]["rank_reason"])

    # --- the cache is not shared between providers or models ---------------------------------------------
    def test_gignatis_cached_answer_is_not_used_by_openai(self):
        self.put_cache("strategy consultant", "hr", 1, 0, provider="gignati", model=st.GIGNATI_MODEL)
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(len(self.srv.requests), 1)
        self.assertEqual(self.rows(1)[0]["title_function"], "tech")
        self.assertEqual({(r["provider"], r["model"]) for r in self.cache_rows()},
                         {("gignati", st.GIGNATI_MODEL), ("openai", "gpt-4.1-nano")})

    def test_another_model_of_the_same_provider_is_not_reused(self):
        self.put_cache("strategy consultant", "hr", 1, 0, provider="openai", model="gpt-4.1-mini")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(len(self.srv.requests), 1)

    def test_a_row_for_the_same_provider_model_and_version_is_reused(self):
        self.put_cache("strategy consultant", "legal", 3, 3, provider="openai", model="gpt-4.1-nano")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        counts = ranking.run()
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(self.rows(1)[0]["title_function"], "legal")
        self.assertEqual(counts["cache_hits"], 1)

    def test_another_prompt_version_is_not_reused_even_for_the_same_provider_and_model(self):
        self.put_cache("strategy consultant", "hr", 1, 0, version="old", provider="openai", model="gpt-4.1-nano")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(len(self.srv.requests), 1)
        keys = {(r["provider"], r["model"], r["prompt_version"]) for r in self.cache_rows()}
        self.assertEqual(keys, {("openai", "gpt-4.1-nano", st.PROMPT_VERSION)}, "the stale row is replaced in place")

    def test_each_provider_keeps_its_own_row_for_one_title(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        with mock.patch.object(st, "TITLE_PROVIDER", "gignati"):
            self.srv.answers["Strategy Consultant"] = ("other", 2, 1)
            self.reported(2, "SMB")
            self.contact(2, "Strategy Consultant")
            ranking.run()
        rows = {r["provider"]: r["function"] for r in self.cache_rows() if r["source"] == "llm"}
        self.assertEqual(rows, {"openai": "tech", "gignati": "other"})

    def test_a_rule_row_and_an_llm_row_for_one_title_coexist(self):
        self.put_cache("head of it", "tech", 5, 5, provider=None, model=None, source="rule")
        self.put_cache("head of it", "hr", 1, 1)
        self.assertEqual(len(self.cache_rows()), 2)

    # --- the privacy rule: only the title text leaves -------------------------------------------------------
    def test_nothing_but_the_title_is_sent_no_email_name_company_or_domain(self):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, domain, status, company_scale, website)
                           VALUES (77, 'secretco.example.com', 'reported', 'SMB', 'https://www.secretco.example.com')""")
        cid = str(uuid.uuid4())
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, job_title, first_name, check_passed)
                           VALUES (%s, 77, 'priya.sharma@secretco.example.com', 'Strategy Consultant', 'Priya', TRUE)""",
                        (cid,))
        ranking.run()
        self.assertEqual(len(self.srv.requests), 1)
        req = self.srv.requests[0]
        blob = json.dumps(req["body"]) + json.dumps({k: v for k, v in req["headers"].items()
                                                     if k.lower() != "authorization"}) + req["path"]
        for secret in ("priya", "sharma", "Priya", "secretco", "example.com", "77", cid, "SMB"):
            self.assertNotIn(secret, blob, secret)
        self.assertEqual(req["body"]["messages"][1]["content"], "Strategy Consultant")
        self.assertEqual(set(req["body"]), {"model", "messages", "temperature", "max_completion_tokens",
                                            "response_format"})

    def test_the_title_sent_is_trimmed_and_capped(self):
        long_title = "Strategy   Consultant  " + "x" * 300
        self.reported(1, "SMB")
        self.contact(1, long_title)
        ranking.run()
        sent = self.srv.requests[0]["body"]["messages"][1]["content"]
        self.assertTrue(sent.startswith("Strategy Consultant xxx"))
        self.assertLessEqual(len(sent), 120)

    # --- failure modes -----------------------------------------------------------------------------------------
    def test_invalid_output_retries_once_then_unclassified(self):
        bad = ("content", "nonsense")
        self.srv.script = [bad, bad]
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        self.contact(1, "CTO")
        counts = ranking.run()
        self.assertEqual(len(self.srv.requests), 2)
        self.assertEqual(self.by_title(1)["Strategy Consultant"]["title_source"], "unclassified")
        self.assertEqual(counts["titles_unclassified"], 1)
        self.assertEqual([r for r in self.cache_rows() if r["source"] == "llm"], [])

    def test_rate_limited_leaves_the_company_at_reported_and_stops_calling(self):
        self.srv.script = [("status", 429)] * 3
        for i in (1, 2, 3):
            self.reported(i, "SMB")
            self.contact(i, "Strategy Consultant")
        counts = ranking.run()
        self.assertEqual(len(self.srv.requests), 1)
        for i in (1, 2, 3):
            self.assertEqual(self.status_of(i)[0], "reported")
            self.assertIsNone(self.rows(i)[0]["rank"])
        self.assertEqual(counts["deferred_llm_unavailable"], 3)
        self.assertEqual(counts["held_no_contact"], 0)

    def test_a_down_openai_is_retried_next_run(self):
        with mock.patch.object(st, "OPENAI_BASE_URL", dead_url() + "/v1"):
            self.reported(1, "SMB")
            self.contact(1, "Strategy Consultant")
            ranking.run()
        self.assertEqual(self.status_of(1)[0], "reported")
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_a_missing_key_leaves_llm_companies_at_reported_and_reports_it(self):
        os.environ.pop("OPENAI_API_KEY")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        self.reported(2, "SMB")
        self.contact(2, "Product Manager")
        counts = ranking.run()
        for i in (1, 2):
            self.assertEqual(self.status_of(i)[0], "reported")
            self.assertIsNone(self.rows(i)[0]["rank"])
            self.assertIsNone(self.rows(i)[0]["title_source"])
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(counts["deferred_no_api_key"], 2)
        self.assertEqual(counts["deferred_llm_unavailable"], 0)
        self.assertEqual(counts["held_no_contact"], 0)
        self.assertEqual(counts["llm_calls"], 0)

    def test_a_missing_key_never_counts_as_no_contact(self):
        os.environ.pop("OPENAI_API_KEY")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertNotEqual(self.status_of(1)[0], "held_no_contact")

    def test_rule_matched_companies_still_proceed_without_a_key(self):
        os.environ.pop("OPENAI_API_KEY")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        self.reported(2, "SMB")
        self.contact(2, "CTO")
        counts = ranking.run()
        self.assertEqual(self.status_of(1)[0], "reported")
        self.assertEqual(self.status_of(2)[0], "contact_ranked")
        self.assertEqual(counts["contact_ranked"], 1)

    def test_a_cached_title_still_works_without_a_key(self):
        os.environ.pop("OPENAI_API_KEY")
        self.put_cache("strategy consultant", "tech", 5, 4, provider="openai", model="gpt-4.1-nano")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_a_dry_run_does_not_need_a_key_and_sends_nothing(self):
        os.environ.pop("OPENAI_API_KEY")
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        counts = ranking.run(dry_run=True)
        self.assertEqual(counts["would_need_llm"], ["Strategy Consultant"])
        self.assertEqual(self.srv.requests, [])
        self.assertEqual(counts["deferred_no_api_key"], 0)

    def test_the_key_is_never_written_to_the_database(self):
        self.srv.answers["Strategy Consultant"] = ("tech", 5, 4)
        self.reported(1, "SMB")
        self.contact(1, "Strategy Consultant")
        ranking.run()
        with db.get_cursor() as cur:
            for table in ("ae_title_cache", "ae_contacts", "ae_companies"):
                cur.execute(f"SELECT t::text AS row FROM {table} t")
                for r in cur.fetchall():
                    self.assertNotIn(FAKE_KEY, r["row"], table)



if __name__ == "__main__":
    unittest.main()
