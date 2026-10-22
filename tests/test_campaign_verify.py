"""
Stage 8: NeverBounce verification and marking (campaign/verify.py, campaign/neverbounce.py).

    run.sh api python -m unittest tests.test_campaign_verify -v

Database tests use the throwaway-database DbCase from tests.test_campaign. The live client is
tested against a local fake NeverBounce server (a thread); nothing here reaches the real API,
and mock mode is proven by making the network raise.
"""
import json
import sys
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import crm_write, neverbounce, settings, settings_verify, status, verify
from tests.test_campaign import DbCase

REPORT_URL = "https://reports.consiva.tech/r/" + "a" * 32 + ".pdf"


# --- a fake NeverBounce -------------------------------------------------------------
class FakeNB(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.results = {}          # email -> result string
        self.default = "valid"
        self.fail = None           # None | ("http", 500) | ("json", "temp_unavail") | ("json", "auth_failure")
        self.fail_on = "check"     # "check" = only /single/check fails; "all" = every endpoint
        self.credits = {"paid_credits_remaining": 1000, "free_credits_remaining": 0}
        self.calls = []            # (path, query dict)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}/v4"

    def checks(self):
        return [q["email"][0] for p, q in self.calls if p.endswith("/single/check")]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        srv = self.server
        srv.calls.append((u.path, q))
        failing = srv.fail and (srv.fail_on == "all" or u.path.endswith("/single/check"))
        if failing and srv.fail[0] == "http":
            return self._send(srv.fail[1], {"status": "error"})
        if failing and srv.fail[0] == "json":
            return self._send(200, {"status": srv.fail[1], "message": "scripted failure"})
        if u.path.endswith("/account/info"):
            return self._send(200, {"status": "success", "credits_info": srv.credits})
        if u.path.endswith("/single/check"):
            email = q["email"][0]
            return self._send(200, {"status": "success", "result": srv.results.get(email, srv.default)})
        self._send(404, {"status": "error"})


# --- helpers ---------------------------------------------------------------------------
class VerifyCase(DbCase):
    def setUp(self):
        super().setUp()
        self.mock = neverbounce.MockClient({})
        self.patches = [mock.patch.object(settings_verify, "MODE", "mock"),
                        mock.patch.object(settings_verify, "DAILY_CREDIT_CAP", 200)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def company(self, cid, status_="contact_ranked", is_test=True, report_url=REPORT_URL,
                age_days=1, **kw):
        report_date = None if age_days is None else datetime.now(timezone.utc) - timedelta(days=age_days)
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, report_url, report_date)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (cid, is_test, f"c{cid}.example.com", status_, report_url, report_date))

    def contact(self, cid, email, rank, passed=True, **kw):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, rank, check_passed)
                           VALUES (%s, %s, %s, %s, %s)""", (f"k{cid}-{rank}", cid, email, rank, passed))

    def staffed(self, cid, emails, **kw):
        """A contact_ranked company whose contacts are ranked in the order given."""
        self.company(cid, **kw)
        for rank, email in enumerate(emails, start=1):
            self.contact(cid, email, rank)

    def co(self, cid):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_companies WHERE company_id = %s", (cid,))
            return dict(cur.fetchone())

    def ct(self, cid, rank):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_contacts WHERE contact_id = %s", (f"k{cid}-{rank}",))
            return dict(cur.fetchone())

    def suppressed(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT email, reason, source FROM ae_suppression ORDER BY email")
            return [dict(r) for r in cur.fetchall()]

    def usage(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(credits_used), 0) n FROM ae_neverbounce_usage")
            return cur.fetchone()["n"]

    def cache(self, email=None):
        with db.get_cursor() as cur:
            cur.execute("SELECT email, verdict FROM ae_verification_cache ORDER BY email")
            rows = {r["email"]: r["verdict"] for r in cur.fetchall()}
        return rows if email is None else rows.get(email)

    def run_stage(self, client=None, **kw):
        client = client or self.mock
        if not client.live:                          # a mock verdict may only touch the test cohort
            kw.setdefault("only_test", True)
        return verify.run(client=client, **kw)


class LiveCase(VerifyCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = FakeNB()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        s = self.srv
        s.results, s.default, s.fail, s.fail_on, s.calls = {}, "valid", None, "check", []
        s.credits = {"paid_credits_remaining": 1000, "free_credits_remaining": 0}
        self.live = neverbounce.LiveClient("test-key", base_url=s.base, timeout=5)


# --- settings and the client ------------------------------------------------------------
class Settings(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(settings_verify.MODE, "mock")
        self.assertEqual(settings_verify.CACHE_DAYS, 45)
        self.assertEqual(settings_verify.REPORT_FRESH_DAYS, 14)
        self.assertEqual(settings_verify.FALLBACKS, 2)
        self.assertEqual(settings_verify.API_BASE, "https://api.neverbounce.com/v4")

    def test_the_api_key_comes_from_the_environment_only(self):
        src = (ROOT / "campaign" / "settings_verify.py").read_text()
        self.assertIn('os.environ', src)
        with mock.patch.dict("os.environ", {"NEVERBOUNCE_API_KEY": "abc"}):
            self.assertEqual(settings_verify.api_key(), "abc")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(settings_verify.api_key())

    def test_no_key_literal_in_the_repo_sources(self):
        for name in ("settings_verify.py", "neverbounce.py", "verify.py"):
            self.assertNotRegex((ROOT / "campaign" / name).read_text(), r"private_[0-9a-f]{20,}")


class MockClientTests(unittest.TestCase):
    def test_default_verdict_and_overrides(self):
        c = neverbounce.MockClient({"a@x.com": "invalid", "y.com": "catchall"}, default="valid")
        self.assertEqual(c.check("a@x.com"), "invalid")
        self.assertEqual(c.check("b@y.com"), "catchall")      # a domain entry covers its addresses
        self.assertEqual(c.check("c@z.com"), "valid")
        self.assertFalse(c.live)

    def test_it_never_touches_the_network(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")):
            c = neverbounce.MockClient({})
            c.check("a@x.com")
            self.assertIsNone(c.credits_remaining())

    def test_make_client_by_mode(self):
        self.assertIsNone(neverbounce.make_client("off"))
        self.assertIsInstance(neverbounce.make_client("mock"), neverbounce.MockClient)
        with self.assertRaises(ValueError):
            neverbounce.make_client("sometimes")

    def test_live_without_a_key_is_a_clear_error_and_no_call(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")):
            with self.assertRaises(neverbounce.NeverBounceError) as cm:
                neverbounce.make_client("live")
        self.assertIn("NEVERBOUNCE_API_KEY", str(cm.exception))


class LiveClientTests(LiveCase):
    def test_every_documented_result_maps_through(self):
        for result in ("valid", "invalid", "disposable", "catchall", "unknown"):
            self.srv.results["a@x.com"] = result
            self.assertEqual(self.live.check("a@x.com"), result)

    def test_it_sends_the_key_and_email_to_the_single_check_endpoint(self):
        self.live.check("a+b@x.com")
        path, q = self.srv.calls[-1]
        self.assertTrue(path.endswith("/v4/single/check"))
        self.assertEqual(q["key"], ["test-key"])
        self.assertEqual(q["email"], ["a+b@x.com"])

    def test_credits_remaining_sums_paid_and_free(self):
        self.srv.credits = {"paid_credits_remaining": 7, "free_credits_remaining": 3}
        self.assertEqual(self.live.credits_remaining(), 10)
        self.assertTrue(self.srv.calls[-1][0].endswith("/v4/account/info"))

    def test_server_errors_and_throttling_are_transient(self):
        for fail in (("http", 500), ("http", 503), ("http", 429), ("json", "temp_unavail"),
                     ("json", "throttle_triggered")):
            self.srv.fail = fail
            with self.assertRaises(neverbounce.TransientError, msg=str(fail)):
                self.live.check("a@x.com")

    def test_a_timeout_is_transient(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            with self.assertRaises(neverbounce.TransientError):
                self.live.check("a@x.com")

    def test_a_refused_connection_is_transient(self):
        dead = neverbounce.LiveClient("k", base_url="http://127.0.0.1:9/v4", timeout=2)
        with self.assertRaises(neverbounce.TransientError):
            dead.check("a@x.com")

    def test_auth_failure_is_permanent(self):
        self.srv.fail = ("json", "auth_failure")
        with self.assertRaises(neverbounce.NeverBounceError):
            self.live.check("a@x.com")

    def test_an_unexpected_result_is_not_guessed_at(self):
        self.srv.results["a@x.com"] = "perhaps"
        with self.assertRaises(neverbounce.TransientError):
            self.live.check("a@x.com")


# --- the gate ----------------------------------------------------------------------------
class Gate(VerifyCase):
    def test_a_company_that_is_not_contact_ranked_is_left_alone(self):
        sts = ("reported", "verified", "gate_passed", "pending")
        for i, st in enumerate(sts):
            self.staffed(10 + i, [f"a{i}@x.example.com"], status_=st)
        self.run_stage()
        self.assertEqual(self.mock.calls, [])
        for i, st in enumerate(sts):
            self.assertEqual(self.co(10 + i)["status"], st)

    def test_a_contact_that_failed_stage_2_is_never_verified(self):
        self.company(1)
        self.contact(1, "a@x.example.com", 1, passed=False)
        counts = self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertIn("stage 2", self.co(1)["status_reason"])
        self.assertEqual(self.ct(1, 1)["nb_decision"], "NOT_CHECKED")
        self.assertEqual(counts["blocked"], {"no_eligible_contact": 1})

    def test_only_a_passing_contact_is_verified_when_the_top_one_failed(self):
        self.company(1)
        self.contact(1, "bad@x.example.com", 1, passed=False)
        self.contact(1, "ok@x.example.com", 2, passed=True)
        self.run_stage()
        self.assertEqual(self.mock.calls, ["ok@x.example.com"])
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-2")

    def test_a_company_with_no_report_is_blocked(self):
        self.staffed(1, ["a@x.example.com"], report_url=None)
        counts = self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertIn("report", self.co(1)["status_reason"])
        self.assertEqual(counts["blocked"], {"no_report": 1})

    def test_a_report_older_than_14_days_is_blocked(self):
        self.staffed(1, ["a@x.example.com"], age_days=15)
        counts = self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertIn("14", self.co(1)["status_reason"])
        self.assertEqual(counts["blocked"], {"report_stale": 1})

    def test_a_report_13_days_old_is_fine(self):
        self.staffed(1, ["a@x.example.com"], age_days=13)
        self.run_stage()
        self.assertEqual(self.co(1)["status"], "verified")

    def test_a_report_with_no_date_is_blocked(self):
        self.staffed(1, ["a@x.example.com"], age_days=None)
        self.run_stage()
        self.assertEqual(self.co(1)["status"], "contact_ranked")

    def test_a_suppressed_address_is_never_sent_to_the_api(self):
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES ('top@x.example.com','unsubscribed','manual')")
        self.staffed(1, ["Top@X.example.com", "next@x.example.com"])
        self.run_stage()
        self.assertEqual(self.mock.calls, ["next@x.example.com"])
        self.assertEqual(self.ct(1, 1)["nb_decision"], "DO_NOT_EMAIL")
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-2")

    def test_a_company_whose_only_contact_is_suppressed_is_suppressed(self):
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES ('a@x.example.com','bounced','crm_history')")
        self.staffed(1, ["a@x.example.com"])
        self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.co(1)["status"], "suppressed")

    def test_the_daily_cap_used_up_stops_the_stage_before_any_call(self):
        fake = FakeLiveStub()
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_neverbounce_usage (day, credits_used) VALUES (%s, 5)", (verify.today_ist(),))
        with mock.patch.object(settings_verify, "DAILY_CREDIT_CAP", 5):
            self.staffed(1, ["a@x.example.com"])
            counts = self.run_stage(client=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(counts["stopped"], "daily_cap")
        self.assertEqual(self.usage(), 5)


class FakeLiveStub(neverbounce.MockClient):
    """A mock that behaves like a live client for credit purposes (spends credits, reports a balance)."""
    live = True

    def __init__(self, verdicts=None, balance=10_000, default="valid"):
        super().__init__(verdicts or {}, default=default)
        self.balance = balance

    def credits_remaining(self):
        return self.balance


# --- verdict mapping and fallbacks ---------------------------------------------------------
class Verdicts(VerifyCase):
    def run_one(self, verdict):
        self.mock = neverbounce.MockClient({"a@x.example.com": verdict})
        self.staffed(1, ["a@x.example.com"])
        return self.run_stage()

    def test_valid_is_ok_and_verified(self):
        self.run_one("valid")
        self.assertEqual(self.ct(1, 1)["nb_decision"], "OK")
        self.assertEqual(self.ct(1, 1)["nb_verdict"], "valid")
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertFalse(self.co(1)["risky"])

    def test_catchall_and_unknown_are_risky_but_accepted(self):
        for i, v in enumerate(("catchall", "unknown")):
            self.mock = neverbounce.MockClient({f"a{i}@x.example.com": v})
            self.staffed(10 + i, [f"a{i}@x.example.com"])
            self.run_stage()
            self.assertEqual(self.ct(10 + i, 1)["nb_decision"], "RISKY", v)
            self.assertEqual(self.ct(10 + i, 1)["nb_verdict"], v)
            self.assertEqual(self.co(10 + i)["status"], "verified", v)
            self.assertTrue(self.co(10 + i)["risky"], v)

    def test_invalid_and_disposable_are_do_not_email_and_suppressed(self):
        for i, v in enumerate(("invalid", "disposable")):
            fake = FakeLiveStub({f"a{i}@x.example.com": v})
            self.staffed(10 + i, [f"a{i}@x.example.com"])
            self.run_stage(client=fake)
            self.assertEqual(self.ct(10 + i, 1)["nb_decision"], "DO_NOT_EMAIL", v)
            self.assertEqual(self.ct(10 + i, 1)["nb_verdict"], v)
            self.assertEqual(self.co(10 + i)["status"], "suppressed", v)
        self.assertEqual(self.suppressed(), [
            {"email": "a0@x.example.com", "reason": "invalid", "source": "neverbounce"},
            {"email": "a1@x.example.com", "reason": "invalid", "source": "neverbounce"}])

    def test_the_verdict_check_time_and_cache_are_stored(self):
        self.run_one("valid")
        self.assertIsNotNone(self.ct(1, 1)["nb_checked_at"])
        self.assertEqual(self.cache("a@x.example.com"), "valid")

    def test_the_chosen_contact_is_recorded(self):
        self.run_one("valid")
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-1")

    def test_valid_first_means_no_further_calls(self):
        self.staffed(1, ["a@x.example.com", "b@x.example.com", "c@x.example.com"])
        self.run_stage()
        self.assertEqual(self.mock.calls, ["a@x.example.com"])
        self.assertEqual(self.ct(1, 2)["nb_decision"], "NOT_CHECKED")

    def test_catchall_is_accepted_without_checking_further_contacts(self):
        self.mock = neverbounce.MockClient({"a@x.example.com": "catchall"})
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        self.run_stage()
        self.assertEqual(self.mock.calls, ["a@x.example.com"])
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-1")
        self.assertEqual(self.ct(1, 2)["nb_decision"], "NOT_CHECKED")

    def test_invalid_falls_back_to_the_next_ranked_contact(self):
        self.mock = neverbounce.MockClient({"a@x.example.com": "invalid"})
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        self.run_stage()
        self.assertEqual(self.mock.calls, ["a@x.example.com", "b@x.example.com"])
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-2")
        self.assertEqual(self.ct(1, 1)["nb_decision"], "DO_NOT_EMAIL")

    def test_fallback_stops_at_two_more_then_the_company_is_suppressed(self):
        self.mock = FakeLiveStub({}, default="invalid")
        self.staffed(1, [f"c{i}@x.example.com" for i in range(1, 6)])
        self.run_stage()
        self.assertEqual(len(self.mock.calls), 3, "the top contact plus 2 fallbacks, no more")
        self.assertEqual(self.co(1)["status"], "suppressed")
        self.assertEqual(len(self.suppressed()), 3)
        self.assertEqual(self.ct(1, 4)["nb_decision"], "NOT_CHECKED")
        self.assertEqual(self.ct(1, 5)["nb_decision"], "NOT_CHECKED")

    def test_the_second_fallback_can_still_succeed(self):
        self.mock = neverbounce.MockClient({"c1@x.example.com": "invalid", "c2@x.example.com": "invalid"})
        self.staffed(1, [f"c{i}@x.example.com" for i in range(1, 5)])
        self.run_stage()
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-3")
        self.assertEqual(len(self.mock.calls), 3)

    def test_a_fallback_that_is_only_risky_still_makes_the_company_verified_and_risky(self):
        self.mock = neverbounce.MockClient({"c1@x.example.com": "invalid", "c2@x.example.com": "catchall"})
        self.staffed(1, ["c1@x.example.com", "c2@x.example.com", "c3@x.example.com"])
        self.run_stage()
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertTrue(self.co(1)["risky"])
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-2")
        self.assertEqual(len(self.mock.calls), 2)

    def test_contacts_with_no_rank_are_not_verified(self):
        self.company(1)
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, rank, check_passed)
                           VALUES ('k1-x', 1, 'unranked@x.example.com', NULL, TRUE)""")
        self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.co(1)["status"], "contact_ranked")


# --- credits: cap, balance, cache --------------------------------------------------------
class Credits(VerifyCase):
    def test_a_live_style_run_records_each_credit_in_the_ledger(self):
        fake = FakeLiveStub({"a1@x.example.com": "invalid"})
        self.staffed(1, ["a1@x.example.com", "b1@x.example.com"])
        self.staffed(2, ["a2@x.example.com"])
        counts = self.run_stage(client=fake)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(self.usage(), 3)
        self.assertEqual(counts["credits_used"], 3)
        self.assertEqual(counts["credits_a_live_run_would_use"], 3)

    def test_the_daily_cap_is_never_exceeded_and_the_stage_stops_cleanly(self):
        fake = FakeLiveStub()
        for cid in (1, 2, 3):
            self.staffed(cid, [f"a{cid}@x.example.com"])
        with mock.patch.object(settings_verify, "DAILY_CREDIT_CAP", 2):
            counts = self.run_stage(client=fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(self.usage(), 2)
        self.assertEqual(counts["stopped"], "daily_cap")
        statuses = sorted(self.co(c)["status"] for c in (1, 2, 3))
        self.assertEqual(statuses, ["contact_ranked", "verified", "verified"])

    def test_a_cap_hit_in_the_middle_of_a_company_keeps_what_was_learned_and_resumes(self):
        fake = FakeLiveStub({"a@x.example.com": "invalid"})
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        with mock.patch.object(settings_verify, "DAILY_CREDIT_CAP", 1):
            self.run_stage(client=fake)
        self.assertEqual(self.ct(1, 1)["nb_decision"], "DO_NOT_EMAIL")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(self.usage(), 1)
        with db.get_cursor() as cur:                      # "tomorrow"
            cur.execute("UPDATE ae_neverbounce_usage SET day = day - 1")
        fake2 = FakeLiveStub()
        self.run_stage(client=fake2)
        self.assertEqual(fake2.calls, ["b@x.example.com"], "the invalid contact is not paid for twice")
        self.assertEqual(self.co(1)["chosen_contact_id"], "k1-2")
        self.assertEqual(self.co(1)["status"], "verified")

    def test_the_cap_counts_the_ist_calendar_day(self):
        fake = FakeLiveStub()
        self.staffed(1, ["a@x.example.com"])
        with mock.patch.object(settings_verify, "DAILY_CREDIT_CAP", 1), \
                mock.patch.object(verify, "today_ist", lambda: datetime(2026, 10, 12).date()):
            self.run_stage(client=fake)
        with db.get_cursor() as cur:
            cur.execute("SELECT day, credits_used FROM ae_neverbounce_usage")
            self.assertEqual([(str(r["day"]), r["credits_used"]) for r in cur.fetchall()], [("2026-10-12", 1)])

    def test_a_low_balance_stops_the_stage_before_any_call(self):
        fake = FakeLiveStub(balance=2)
        for cid in (1, 2, 3):
            self.staffed(cid, [f"a{cid}@x.example.com"])
        counts = self.run_stage(client=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(counts["stopped"], "low_balance")
        self.assertEqual(self.usage(), 0)
        self.assertTrue(all(self.co(c)["status"] == "contact_ranked" for c in (1, 2, 3)))

    def test_a_balance_that_covers_the_run_goes_ahead(self):
        fake = FakeLiveStub(balance=3)
        for cid in (1, 2, 3):
            self.staffed(cid, [f"a{cid}@x.example.com"])
        counts = self.run_stage(client=fake)
        self.assertIsNone(counts["stopped"])
        self.assertEqual(len(fake.calls), 3)

    def test_the_balance_running_out_mid_run_stops_cleanly(self):
        class Draining(FakeLiveStub):
            def check(self, email):
                self.balance -= 1
                return super().check(email)
        fake = Draining(balance=4, default="invalid")     # best case needs 2, so the run starts
        for cid in (1, 2):
            self.staffed(cid, [f"a{cid}@x.example.com", f"b{cid}@x.example.com", f"c{cid}@x.example.com"])
        counts = self.run_stage(client=fake)
        self.assertEqual(len(fake.calls), 4, "never a call the balance cannot pay for")
        self.assertEqual(counts["stopped"], "low_balance")
        self.assertEqual(self.co(1)["status"], "suppressed")
        self.assertEqual(self.co(2)["status"], "contact_ranked")
        self.assertEqual(self.usage(), 4)

    def test_a_cached_verdict_under_45_days_spends_nothing(self):
        fake = FakeLiveStub()
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_verification_cache (email, verdict, checked_at)
                           VALUES ('a@x.example.com', 'valid', NOW() - INTERVAL '44 days')""")
        self.staffed(1, ["a@x.example.com"])
        counts = self.run_stage(client=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.usage(), 0)
        self.assertEqual(counts["cache_hits"], 1)
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.ct(1, 1)["nb_verdict"], "valid")

    def test_a_cached_verdict_over_45_days_is_rechecked_and_refreshed(self):
        fake = FakeLiveStub({"a@x.example.com": "catchall"})
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_verification_cache (email, verdict, checked_at)
                           VALUES ('a@x.example.com', 'valid', NOW() - INTERVAL '46 days')""")
        self.staffed(1, ["a@x.example.com"])
        self.run_stage(client=fake)
        self.assertEqual(fake.calls, ["a@x.example.com"])
        self.assertEqual(self.usage(), 1)
        self.assertEqual(self.cache("a@x.example.com"), "catchall")

    def test_a_cached_invalid_is_do_not_email_without_a_call(self):
        fake = FakeLiveStub()
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_verification_cache (email, verdict, checked_at, source)
                           VALUES ('a@x.example.com', 'invalid', NOW() - INTERVAL '3 days', 'live')""")
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        self.run_stage(client=fake)
        self.assertEqual(fake.calls, ["b@x.example.com"])
        self.assertEqual(self.ct(1, 1)["nb_decision"], "DO_NOT_EMAIL")
        self.assertIn("a@x.example.com", [s["email"] for s in self.suppressed()])

    def test_cache_hits_are_shared_between_companies(self):
        fake = FakeLiveStub()
        self.staffed(1, ["shared@x.example.com"])
        self.staffed(2, ["shared@x.example.com"])
        self.run_stage(client=fake)
        self.assertEqual(fake.calls, ["shared@x.example.com"], "second company reuses the first verdict")
        self.assertEqual(self.usage(), 1)


# --- failures of the API ---------------------------------------------------------------------
class ApiFailures(LiveCase):
    def run_live(self, **kw):
        with mock.patch.object(settings_verify, "MODE", "live"):
            return self.run_stage(client=self.live, **kw)

    def snapshot(self):
        return (self.suppressed(), self.usage(), self.cache(),
                [self.co(c)["status"] for c in (1, 2)])

    def test_a_server_error_leaves_the_company_retryable_and_marks_nothing(self):
        self.srv.fail = ("http", 500)
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        self.staffed(2, ["c@x.example.com"])
        counts = self.run_live()
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(self.ct(1, 1)["nb_decision"], "NOT_CHECKED")
        self.assertEqual(self.suppressed(), [])
        self.assertEqual(self.usage(), 0, "a credit reserved for a failed call is given back")
        self.assertEqual(counts["stopped"], "transient_error")
        self.assertEqual(len(self.srv.checks()), 1, "stops hammering a failing API")

    def test_rate_limiting_and_temp_unavailable_are_handled_the_same_way(self):
        self.staffed(50, ["z@x.example.com"])
        for fail in (("http", 429), ("json", "temp_unavail"), ("json", "throttle_triggered")):
            self.srv.fail = fail
            self.srv.calls.clear()
            counts = self.run_live()
            self.assertEqual(self.co(50)["status"], "contact_ranked", str(fail))
            self.assertEqual(self.suppressed(), [], str(fail))
            self.assertEqual(counts["stopped"], "transient_error", str(fail))

    def test_a_failed_call_after_an_invalid_keeps_the_invalid_and_does_not_suppress_the_next(self):
        self.srv.results["a@x.example.com"] = "invalid"
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        calls = {"n": 0}
        real = self.live.check

        def flaky(email):
            calls["n"] += 1
            if calls["n"] == 2:
                raise neverbounce.TransientError("boom")
            return real(email)
        self.live.check = flaky
        self.run_live()
        self.assertEqual(self.ct(1, 1)["nb_decision"], "DO_NOT_EMAIL")
        self.assertEqual(self.ct(1, 2)["nb_decision"], "NOT_CHECKED")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual([s["email"] for s in self.suppressed()], ["a@x.example.com"])
        self.assertEqual(self.usage(), 1)

    def test_the_balance_call_failing_stops_the_stage_before_any_check(self):
        self.srv.fail, self.srv.fail_on = ("http", 500), "all"
        self.staffed(1, ["a@x.example.com"])
        counts = self.run_live()
        self.assertEqual(counts["stopped"], "transient_error")
        self.assertEqual(self.srv.checks(), [])
        self.assertEqual(self.co(1)["status"], "contact_ranked")

    def test_an_auth_failure_stops_the_stage_and_marks_nothing(self):
        self.srv.fail = ("json", "auth_failure")
        self.staffed(1, ["a@x.example.com"])
        counts = self.run_live()
        self.assertEqual(counts["stopped"], "api_error")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(self.suppressed(), [])
        self.assertEqual(self.usage(), 0)

    def test_the_balance_is_read_from_the_account_endpoint_before_any_check(self):
        self.srv.credits = {"paid_credits_remaining": 0, "free_credits_remaining": 1}
        for cid in (1, 2):
            self.staffed(cid, [f"a{cid}@x.example.com"])
        counts = self.run_live()
        paths = [p for p, _ in self.srv.calls]
        self.assertTrue(paths[0].endswith("/account/info"))
        self.assertEqual(self.srv.checks(), [])
        self.assertEqual(counts["stopped"], "low_balance")

    def test_end_to_end_through_the_live_client(self):
        self.srv.results = {"a@x.example.com": "invalid", "b@x.example.com": "catchall"}
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        counts = self.run_live()
        self.assertEqual(self.srv.checks(), ["a@x.example.com", "b@x.example.com"])
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertTrue(self.co(1)["risky"])
        self.assertEqual(self.usage(), 2)
        self.assertEqual(counts["credits_used"], 2)


# --- modes, dry run, scope ----------------------------------------------------------------------
class Modes(VerifyCase):
    def test_mock_mode_never_calls_the_network_and_counts_what_a_live_run_would_use(self):
        self.mock = neverbounce.MockClient({"a1@x.example.com": "invalid"})
        self.staffed(1, ["a1@x.example.com", "b1@x.example.com"])
        self.staffed(2, ["a2@x.example.com"])
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_verification_cache (email, verdict) VALUES ('b1@x.example.com', 'valid')""")
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")):
            counts = self.run_stage()
        self.assertEqual(counts["credits_a_live_run_would_use"], 2, "a1 and a2; b1 was a cache hit")
        self.assertEqual(counts["credits_used"], 0)
        self.assertEqual(self.usage(), 0, "mock mode does not spend the cap")
        self.assertEqual(self.co(1)["status"], "verified")

    def test_mock_mode_is_not_stopped_by_the_cap(self):
        for cid in (1, 2, 3):
            self.staffed(cid, [f"a{cid}@x.example.com"])
        with mock.patch.object(settings_verify, "DAILY_CREDIT_CAP", 1):
            counts = self.run_stage()
        self.assertIsNone(counts["stopped"])
        self.assertEqual(counts["credits_a_live_run_would_use"], 3)

    def test_run_builds_a_mock_client_by_default(self):
        self.staffed(1, ["a@x.example.com"])
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")):
            counts = verify.run(only_test=True)
        self.assertEqual(counts["mode"], "mock")
        self.assertEqual(self.co(1)["status"], "verified")

    def test_off_mode_does_nothing(self):
        self.staffed(1, ["a@x.example.com"])
        with mock.patch.object(settings_verify, "MODE", "off"):
            counts = verify.run()
        self.assertEqual(counts["mode"], "off")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(self.ct(1, 1)["nb_decision"], "NOT_CHECKED")

    def test_dry_run_writes_nothing_but_reports_the_credits_a_live_run_would_use(self):
        self.staffed(1, ["a@x.example.com", "b@x.example.com"])
        self.staffed(2, ["c@x.example.com"])
        self.staffed(3, ["d@x.example.com"], age_days=20)               # blocked, needs no credit
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_verification_cache (email, verdict) VALUES ('c@x.example.com','valid')")
        before = (self.suppressed(), self.cache(), self.usage(),
                  [self.co(c)["status"] for c in (1, 2, 3)], self.ct(1, 1)["nb_decision"])
        counts = self.run_stage(dry_run=True)
        after = (self.suppressed(), self.cache(), self.usage(),
                 [self.co(c)["status"] for c in (1, 2, 3)], self.ct(1, 1)["nb_decision"])
        self.assertEqual(before, after)
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(counts["credits_a_live_run_would_use"], 1)
        self.assertEqual(counts["credits_worst_case"], 2, "company 1 could need both its contacts checked")
        self.assertEqual(counts["blocked"], {"report_stale": 1})
        self.assertTrue(counts["dry_run"])

    def test_only_test_companies_when_asked(self):
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.staffed(2, ["b@x.example.com"], is_test=False)
        self.run_stage(only_test=True)
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.co(2)["status"], "contact_ranked")
        self.run_stage(client=FakeLiveStub(), only_test=False)
        self.assertEqual(self.co(2)["status"], "verified")

    def test_limit_caps_the_companies_taken(self):
        for cid in (1, 2, 3):
            self.staffed(cid, [f"a{cid}@x.example.com"])
        counts = self.run_stage(limit=2)
        self.assertEqual(counts["companies_seen"], 2)
        self.assertEqual(sorted(self.co(c)["status"] for c in (1, 2, 3)),
                         ["contact_ranked", "verified", "verified"])

    def test_a_second_run_does_nothing_more(self):
        self.staffed(1, ["a@x.example.com"])
        self.run_stage()
        self.mock.calls.clear()
        self.run_stage()
        self.assertEqual(self.mock.calls, [])

    def test_no_crm_write_is_ever_attempted(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        self.staffed(2, ["b@x.example.com"], is_test=False)
        self.mock = FakeLiveStub({"b@x.example.com": "invalid"})
        with mock.patch.object(crm_write, "update_company", side_effect=AssertionError("CRM write")), \
                mock.patch.object(crm_write, "_connect", side_effect=AssertionError("CRM connection")):
            self.run_stage()
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.co(2)["status"], "suppressed")

    def test_the_stage_source_has_no_crm_access(self):
        for name in ("verify.py", "neverbounce.py"):
            src = (ROOT / "campaign" / name).read_text()
            self.assertNotIn("crm_write", src, name)
            self.assertNotIn("crm_read", src, name)
            self.assertNotIn("pymssql", src, name)

    def test_every_status_change_goes_through_set_status(self):
        src = (ROOT / "campaign" / "verify.py").read_text()
        self.assertIn("status.set_status", src)
        self.assertNotRegex(src, r"UPDATE\s+ae_companies\s+SET[^;]*\bstatus\s*=\s*%s,\s*status_reason")
        self.assertNotRegex(src, r"SET\s+status\s*=")


# --- where a verdict came from: mock may only ever touch the test cohort ----------------------------
class MockStaysOutOfProduction(VerifyCase):
    MSG = "mock"

    def snapshot(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT company_id, status, status_reason, chosen_contact_id, risky FROM ae_companies ORDER BY 1")
            companies = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT contact_id, nb_verdict, nb_decision, nb_checked_at, nb_source FROM ae_contacts ORDER BY 1")
            contacts = [dict(r) for r in cur.fetchall()]
        return companies, contacts, self.suppressed(), self.cache(), self.usage()

    def test_mock_refuses_to_run_when_production_is_in_scope(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        before = self.snapshot()
        for scope in (None, False):
            counts = verify.run(client=self.mock, only_test=scope)
            self.assertEqual(counts["stopped"], "mock_in_production", scope)
            self.assertIn("mock", counts["error"].lower())
            self.assertIn("production", counts["error"].lower())
            self.assertEqual(counts["companies_seen"], 0)
            self.assertEqual(counts["api_calls"], 0)
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.snapshot(), before, "nothing changed")

    def test_mixed_scope_with_mock_refuses_entirely_the_test_company_too(self):
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.staffed(2, ["b@x.example.com"], is_test=False)
        before = self.snapshot()
        counts = verify.run(client=self.mock, only_test=None)
        self.assertEqual(counts["stopped"], "mock_in_production")
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.co(1)["status"], "contact_ranked")

    def test_the_default_mock_client_is_refused_too(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")):
            counts = verify.run()                           # MODE is mock, scope is everything
        self.assertEqual(counts["stopped"], "mock_in_production")
        self.assertEqual(self.co(1)["status"], "contact_ranked")

    def test_mock_is_allowed_for_the_test_cohort_and_stores_its_source(self):
        self.staffed(1, ["a@x.example.com"], is_test=True)
        counts = verify.run(client=self.mock, only_test=True)
        self.assertNotIn("error", counts)
        self.assertIsNone(counts["stopped"])
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.ct(1, 1)["nb_source"], "mock")
        with db.get_cursor() as cur:
            cur.execute("SELECT source FROM ae_verification_cache WHERE email = 'a@x.example.com'")
            self.assertEqual(cur.fetchone()["source"], "mock")

    def test_a_mock_run_leaves_production_companies_alone_when_scoped_to_test(self):
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.staffed(2, ["b@x.example.com"], is_test=False)
        verify.run(client=self.mock, only_test=True)
        self.assertEqual(self.co(2)["status"], "contact_ranked")
        self.assertEqual(self.mock.calls, ["a@x.example.com"])

    def test_a_live_verdict_stores_its_source_and_lets_production_through(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        self.staffed(2, ["b@x.example.com"], is_test=True)
        counts = verify.run(client=FakeLiveStub(), only_test=None)
        self.assertNotIn("error", counts)
        for cid, email in ((1, "a@x.example.com"), (2, "b@x.example.com")):
            self.assertEqual(self.co(cid)["status"], "verified")
            self.assertEqual(self.ct(cid, 1)["nb_source"], "live")
            self.assertEqual(self.cache(email), "valid")
        with db.get_cursor() as cur:
            cur.execute("SELECT DISTINCT source FROM ae_verification_cache")
            self.assertEqual([r["source"] for r in cur.fetchall()], ["live"])

    def test_off_mode_still_does_nothing_even_with_production_in_scope(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        before = self.snapshot()
        with mock.patch.object(settings_verify, "MODE", "off"):
            counts = verify.run()
        self.assertEqual(counts["mode"], "off")
        self.assertNotIn("error", counts)
        self.assertIsNone(counts["stopped"])
        self.assertEqual(self.snapshot(), before)

    def test_mock_verdicts_never_reach_the_suppression_list(self):
        self.mock = neverbounce.MockClient({"a@x.example.com": "invalid"})
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.run_stage()
        self.assertEqual(self.ct(1, 1)["nb_decision"], "DO_NOT_EMAIL")
        self.assertEqual(self.co(1)["status"], "suppressed")
        self.assertEqual(self.suppressed(), [], "a fake verdict must not suppress a real address in production")

    def test_a_mock_cached_invalid_served_to_a_test_company_does_not_suppress_either(self):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_verification_cache (email, verdict, source)
                           VALUES ('a@x.example.com', 'invalid', 'mock')""")
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.run_stage()
        self.assertEqual(self.suppressed(), [])

    def test_the_normal_return_shape_has_no_error_key(self):
        self.staffed(1, ["a@x.example.com"])
        counts = self.run_stage()
        self.assertNotIn("error", counts)


class CacheSource(VerifyCase):
    def seed(self, source, verdict="valid", days=3, email="a@x.example.com"):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_verification_cache (email, verdict, checked_at, source)
                           VALUES (%s, %s, NOW() - make_interval(days => %s), %s)""",
                        (email, verdict, days, source))

    def cache_source(self, email="a@x.example.com"):
        with db.get_cursor() as cur:
            cur.execute("SELECT verdict, source FROM ae_verification_cache WHERE email = %s", (email,))
            row = cur.fetchone()
        return (row["verdict"], row["source"]) if row else None

    def test_a_mock_row_is_ignored_for_production_and_overwritten_by_the_live_verdict(self):
        self.seed("mock", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=False)
        fake = FakeLiveStub({"a@x.example.com": "catchall"})
        verify.run(client=fake, only_test=False)
        self.assertEqual(fake.calls, ["a@x.example.com"], "the mock verdict was not trusted")
        self.assertEqual(self.cache_source(), ("catchall", "live"))
        self.assertEqual(self.ct(1, 1)["nb_source"], "live")
        self.assertEqual(self.ct(1, 1)["nb_decision"], "RISKY")

    def test_a_row_with_no_source_is_ignored_for_production_too(self):
        self.seed(None, "valid")
        self.staffed(1, ["a@x.example.com"], is_test=False)
        fake = FakeLiveStub()
        verify.run(client=fake, only_test=False)
        self.assertEqual(fake.calls, ["a@x.example.com"])
        self.assertEqual(self.cache_source(), ("valid", "live"))

    def test_a_live_row_is_served_to_production_without_a_call(self):
        self.seed("live", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=False)
        fake = FakeLiveStub()
        counts = verify.run(client=fake, only_test=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(counts["cache_hits"], 1)
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.ct(1, 1)["nb_source"], "live")
        self.assertEqual(self.usage(), 0)

    def test_a_test_company_may_use_a_mock_row_and_it_keeps_its_original_source(self):
        self.seed("mock", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.ct(1, 1)["nb_source"], "mock")
        self.assertEqual(self.cache_source(), ("valid", "mock"), "serving a verdict does not change its source")

    def test_a_test_company_may_use_a_row_with_no_source_which_stays_unknown(self):
        self.seed(None, "valid")
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.run_stage()
        self.assertEqual(self.mock.calls, [])
        self.assertIsNone(self.ct(1, 1)["nb_source"])

    def test_a_test_company_served_a_live_row_records_live(self):
        self.seed("live", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.run_stage()
        self.assertEqual(self.ct(1, 1)["nb_source"], "live")

    def test_the_same_address_serves_a_test_company_from_mock_and_production_from_live_only(self):
        self.seed("mock", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=True)
        self.staffed(2, ["a@x.example.com"], is_test=False)
        fake = FakeLiveStub()
        verify.run(client=fake, only_test=None)
        self.assertEqual(fake.calls, ["a@x.example.com"], "one live check, for the production company")
        self.assertEqual(self.co(1)["status"], "verified")
        self.assertEqual(self.co(2)["status"], "verified")
        self.assertEqual(self.ct(2, 1)["nb_source"], "live")

    def test_a_stored_non_live_decision_on_a_production_contact_is_not_trusted(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        for src in ("mock", None):
            with db.get_cursor() as cur:
                cur.execute("""UPDATE ae_contacts SET nb_decision = 'OK', nb_verdict = 'valid',
                               nb_checked_at = NOW(), nb_source = %s""", (src,))
            fake = FakeLiveStub()
            verify.run(client=fake, only_test=False)
            self.assertEqual(fake.calls, ["a@x.example.com"], f"source {src!r} was re-verified live")
            self.assertEqual(self.ct(1, 1)["nb_source"], "live")
            with db.get_cursor() as cur:
                cur.execute("UPDATE ae_companies SET status = 'contact_ranked', chosen_contact_id = NULL")
            with db.get_cursor() as cur:
                cur.execute("TRUNCATE ae_verification_cache")

    def test_a_stored_live_decision_on_a_production_contact_is_trusted(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        with db.get_cursor() as cur:
            cur.execute("""UPDATE ae_contacts SET nb_decision = 'OK', nb_verdict = 'valid',
                           nb_checked_at = NOW(), nb_source = 'live'""")
        fake = FakeLiveStub()
        verify.run(client=fake, only_test=False)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.co(1)["status"], "verified")

    def test_a_dry_run_counts_a_credit_for_a_production_company_with_only_a_mock_row(self):
        self.seed("mock", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=False)
        self.staffed(2, ["a@x.example.com"], is_test=True)
        counts = verify.run(dry_run=True, only_test=False)
        self.assertEqual(counts["credits_a_live_run_would_use"], 1)
        counts = verify.run(dry_run=True, only_test=True)
        self.assertEqual(counts["credits_a_live_run_would_use"], 0, "a test company may use the mock row")

    def test_a_live_row_costs_a_production_dry_run_nothing(self):
        self.seed("live", "valid")
        self.staffed(1, ["a@x.example.com"], is_test=False)
        self.assertEqual(verify.run(dry_run=True)["credits_a_live_run_would_use"], 0)


class SafetyNet(VerifyCase):
    """Per company: a production company is never verified on a verdict that is not live."""

    def test_the_rule(self):
        for is_test, source, ok in ((False, "live", True), (False, "mock", False), (False, None, False),
                                    (True, "live", True), (True, "mock", True), (True, None, True)):
            self.assertEqual(verify._may_verify({"is_test": is_test}, source), ok, (is_test, source))

    def test_a_production_company_is_not_verified_by_a_mock_run_state(self):
        """Reaching _verify_company with a mock client for a production company (a bug elsewhere) must not verify it."""
        self.staffed(1, ["a@x.example.com"], is_test=False)
        counts = verify._new_counts("mock", False)
        run_state = verify._Run(self.mock, False, verify.today_ist(), None)
        with status.claim("contact_ranked", 10, is_test=False) as (cur, companies):
            contacts = verify._candidates(cur, 1)
            outcome = verify._verify_company(cur, companies[0], contacts, run_state, counts)
        self.assertEqual(outcome, "blocked_non_live")
        self.assertEqual(self.mock.calls, [], "not even asked")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertIsNone(self.co(1)["chosen_contact_id"])
        self.assertEqual(self.ct(1, 1)["nb_decision"], "NOT_CHECKED")

    def test_a_production_company_whose_chosen_verdict_is_not_live_is_not_marked_verified(self):
        """If the up-front filter ever let a non-live verdict through, the per-company check still refuses it."""
        self.staffed(1, ["a@x.example.com"], is_test=False)
        with db.get_cursor() as cur:
            cur.execute("""UPDATE ae_contacts SET nb_decision = 'OK', nb_verdict = 'valid',
                           nb_checked_at = NOW(), nb_source = 'mock'""")
        counts = verify._new_counts("live", False)
        fake = FakeLiveStub()
        run_state = verify._Run(fake, True, verify.today_ist(), None)
        with mock.patch.object(verify, "_trusted", lambda co, source: True):        # the filter fails open
            with status.claim("contact_ranked", 10, is_test=False) as (cur, companies):
                contacts = verify._candidates(cur, 1)
                outcome = verify._verify_company(cur, companies[0], contacts, run_state, counts)
        self.assertEqual(outcome, "blocked_non_live")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertIsNone(self.co(1)["chosen_contact_id"])

    def test_a_blocked_company_is_reported_and_left_retryable_by_the_run(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        with db.get_cursor() as cur:
            cur.execute("""UPDATE ae_contacts SET nb_decision = 'OK', nb_verdict = 'valid',
                           nb_checked_at = NOW(), nb_source = 'mock'""")
        with mock.patch.object(verify, "_trusted", lambda co, source: True):
            counts = verify.run(client=FakeLiveStub(), only_test=False)
        self.assertEqual(counts["blocked"], {"non_live_verdict": 1})
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertIn("live verdict", self.co(1)["status_reason"])


# --- no API key yet: a clean stop, never an exception ------------------------------------------------------
class MissingKey(VerifyCase):
    def run_live_without_a_key(self, **kw):
        with mock.patch.object(settings_verify, "MODE", "live"), \
                mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network used")):
            return verify.run(**kw)

    def snapshot(self):
        return MockStaysOutOfProduction.snapshot(self)

    def test_it_stops_cleanly_for_every_scope_and_changes_nothing(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        self.staffed(2, ["b@x.example.com"], is_test=True)
        before = self.snapshot()
        for scope in (None, True, False):
            counts = self.run_live_without_a_key(only_test=scope)
            self.assertEqual(counts["stopped"], "missing_api_key", scope)
            self.assertIn("NEVERBOUNCE_API_KEY", counts["error"])
            self.assertEqual(counts["companies_seen"], 0)
            self.assertEqual(counts["api_calls"], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(self.co(2)["status"], "contact_ranked")

    def test_the_test_cohort_in_live_mode_without_a_key_also_stops_cleanly(self):
        self.staffed(1, ["a@x.example.com"], is_test=True)
        counts = self.run_live_without_a_key(only_test=True)
        self.assertEqual(counts["stopped"], "missing_api_key")
        self.assertEqual(self.co(1)["status"], "contact_ranked")
        self.assertEqual(self.usage(), 0)

    def test_an_empty_key_counts_as_missing(self):
        self.staffed(1, ["a@x.example.com"], is_test=True)
        with mock.patch.object(settings_verify, "MODE", "live"), \
                mock.patch.dict("os.environ", {"NEVERBOUNCE_API_KEY": ""}):
            counts = verify.run(only_test=True)
        self.assertEqual(counts["stopped"], "missing_api_key")

    def test_a_dry_run_in_live_mode_needs_no_key(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        counts = self.run_live_without_a_key(dry_run=True)
        self.assertNotIn("error", counts)
        self.assertEqual(counts["credits_a_live_run_would_use"], 1)

    def test_the_key_present_runs_normally(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        with mock.patch.object(settings_verify, "MODE", "live"), \
                mock.patch.dict("os.environ", {"NEVERBOUNCE_API_KEY": "k"}), \
                mock.patch.object(neverbounce, "make_client", lambda mode: FakeLiveStub()):
            counts = verify.run()
        self.assertNotIn("error", counts)
        self.assertEqual(self.co(1)["status"], "verified")

    def test_off_mode_without_a_key_is_just_off(self):
        self.staffed(1, ["a@x.example.com"], is_test=False)
        with mock.patch.object(settings_verify, "MODE", "off"), mock.patch.dict("os.environ", {}, clear=True):
            counts = verify.run()
        self.assertNotIn("error", counts)
        self.assertIsNone(counts["stopped"])


if __name__ == "__main__":
    unittest.main()
