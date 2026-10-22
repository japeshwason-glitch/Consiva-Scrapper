"""
Auto Email Campaign, milestone 2, stages 4-6 and the CRM report columns:
scan adapter, scan stage (reuse, retries, unscannable), score gate, report stage, and every
value written to the CRM compared with what the retired report job wrote.

    SP=<scratchpad>; $SP/run.sh api python -m unittest tests.test_campaign_scan -v

Needs the reports libraries (report_build imports yaml); the campaign image has them.
The scanner, the PDF renderer and the R2 upload are replaced by fakes here; nothing opens a
browser, a bucket or the CRM. The real ones run in the sample run.
"""
import asyncio
import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
import db
import report_findings as rf

from campaign import (crm_write, gate, report_stage, scan_adapter, scan_stage, settings, stages,
                      status)
from tests import campaign_fixtures as fx
from tests.test_campaign import DbCase, FakeCrmConn, crm_with, writer

URL = config.REPORT_PUBLIC_BASE_URL + "/r/{}.pdf"
GOOD_URL = URL.format("0123456789abcdef0123456789abcdef")


def scan_result(**kw):
    """What a scanner call hands back: (data, error, seconds)."""
    return fx.scan_data(**kw), None, 1.5


class ScanCase(DbCase):
    def add_checked(self, company_id, domain=None, is_test=False, **kw):
        domain = domain or f"c{company_id}.example.com"
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, domain_source, status,
                                                     company_name, scan_attempts, next_retry_at)
                           VALUES (%s, %s, %s, 'website', 'checked', %s, %s, %s)""",
                        (company_id, is_test, domain, kw.get("name", f"Company {company_id}"),
                         kw.get("attempts", 0), kw.get("next_retry_at")))

    def row(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_companies WHERE company_id = %s", (company_id,))
            return cur.fetchone()

    def scans(self, company_id=None):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM report_scans" + (" WHERE lead_ref = %s" % company_id if company_id else "")
                        + " ORDER BY id")
            return cur.fetchall()

    def seed_scan(self, company_id, domain=None, age_days=0, **fixture):
        """A stored report_scans row, as the scan stage writes it. Returns its id."""
        domain = domain or f"c{company_id}.example.com"
        company = {"company_id": company_id, "company_name": f"Company {company_id}", "domain": domain}
        row = scan_adapter.scan_row_from(company, fx.scan_data(domain=domain, **fixture), None, attempts=1)
        scan_id = db.insert_report_scan(**row)
        with db.get_cursor() as cur:
            cur.execute("UPDATE report_scans SET scanned_at = NOW() - make_interval(days => %s) WHERE id = %s",
                        (age_days, scan_id))
        return scan_id


# --- pure -------------------------------------------------------------------------------
class FailureCategories(unittest.TestCase):
    def test_each_category(self):
        cases = {
            "net::ERR_NAME_NOT_RESOLVED at https://x.example": "dns",
            "getaddrinfo ENOTFOUND x.example": "dns",
            "Page.goto: Timeout 25000ms exceeded": "timeout",
            "homepage did not finish within 150s": "timeout",
            "net::ERR_CONNECTION_TIMED_OUT": "timeout",
            "Request blocked by Cloudflare challenge": "blocked",
            "server answered HTTP 503": "http_error",
            "net::ERR_HTTP_RESPONSE_CODE_FAILURE": "http_error",
            "something odd": "other", "": "other", None: "other"}
        for error, want in cases.items():
            self.assertEqual(scan_adapter.classify_failure(error), want, error)

    def test_the_categories_are_exactly_the_five_the_database_allows(self):
        self.assertEqual(set(scan_adapter.FAILURE_CATEGORIES),
                         {"dns", "timeout", "blocked", "http_error", "other"})

    def test_unscannable_reasons_map_to_a_category(self):
        self.assertEqual(scan_adapter.unscannable_category("homepage returned HTTP 403"), "http_error")
        self.assertEqual(scan_adapter.unscannable_category("homepage is a bot check or challenge page"), "blocked")
        self.assertEqual(scan_adapter.unscannable_category("homepage is parked, a placeholder or unfinished"), "other")


class ScanCommand(unittest.TestCase):
    COMPANY = {"company_id": 7, "company_name": "Acme", "domain": "acme.example.com"}

    def test_the_payload_keeps_the_existing_keying(self):
        p = scan_adapter.payload(self.COMPANY)
        self.assertEqual(p, {"resolved_domain": "acme.example.com", "company_name": "Acme", "cin": "partner:7"})

    def test_it_calls_the_scanners_internal_function_not_the_cli(self):
        argv = scan_adapter.scan_argv(self.COMPANY)
        self.assertNotIn("report_build.py", " ".join(argv))
        self.assertNotIn("--scan-one", argv)
        self.assertIn("_scan_worker", " ".join(argv))

    def test_a_clean_result_is_parsed(self):
        argv = [sys.executable, "-c", "import json; print('noise'); print(json.dumps({'data': {'a': 1}, 'error': None}))"]
        data, err, secs = asyncio.run(scan_adapter.run_scan(self.COMPANY, timeout=20, argv=argv))
        self.assertEqual((data, err), ({"a": 1}, None))
        self.assertGreaterEqual(secs, 0)

    def test_an_error_result_is_returned_as_an_error(self):
        argv = [sys.executable, "-c", "import json; print(json.dumps({'data': None, 'error': 'net::ERR_NAME_NOT_RESOLVED'}))"]
        data, err, _ = asyncio.run(scan_adapter.run_scan(self.COMPANY, timeout=20, argv=argv))
        self.assertIsNone(data)
        self.assertIn("ERR_NAME_NOT_RESOLVED", err)

    def test_a_crashed_worker_is_an_error_not_an_exception(self):
        argv = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"]
        data, err, _ = asyncio.run(scan_adapter.run_scan(self.COMPANY, timeout=20, argv=argv))
        self.assertIsNone(data)
        self.assertIn("exited 3", err)

    def test_a_hung_worker_is_killed_at_the_timeout(self):
        argv = [sys.executable, "-c", "import time; time.sleep(60)"]
        t = time.monotonic()
        data, err, _ = asyncio.run(scan_adapter.run_scan(self.COMPANY, timeout=1, argv=argv))
        self.assertLess(time.monotonic() - t, 20)
        self.assertIsNone(data)
        self.assertIn("did not finish within 1s", err)
        self.assertEqual(scan_adapter.classify_failure(err), "timeout")


class GateDecision(unittest.TestCase):
    def test_band_boundaries(self):
        for score, want in ((0, "A"), (24, "A"), (25, "B"), (50, "B"), (51, "C"), (74, "C"), (75, "D"), (90, "D")):
            self.assertEqual(gate.decide(score, 1), ("gate_passed", want), score)

    def test_over_ninety_is_too_high(self):
        for score in (91, 96, 99, 100):
            self.assertEqual(gate.decide(score, 1), ("score_too_high", None), score)

    def test_zero_gaps_is_no_gaps_whatever_the_score(self):
        for score in (0, 24, 75, 90, 91, 100):
            self.assertEqual(gate.decide(score, 0), ("no_gaps", None), score)

    def test_the_gap_minimum_is_a_setting(self):
        with mock.patch.object(settings, "MIN_GAPS", 2):
            self.assertEqual(gate.decide(40, 1), ("no_gaps", None))
            self.assertEqual(gate.decide(40, 2), ("gate_passed", "B"))


# --- the CRM report columns, against what the retired job wrote -----------------------------
LEGACY = {   # real rows from dbo.CRM_Scrapper, 2026-10-12
    3769: dict(GapScore=0, TrackersPreConsent=16, ThirdPartiesCount=6, CookieBanner="present",
               CollectsViaForm="email — no consent checkbox; submitted to admin.hk.co",
               ConsentFinding="No consent management platform detected; 16 of 16 cookies are tracking cookies set before consent: CLID, _gcl_au, _ga_L6NJDNV4MS, _ga, _ga_PM3QG5MDJ4, _fbp (+10 more); data sent to 6 third-party domains before consent: bing.com, c.bing.com, c.clarity.ms, clarity.ms, facebook.com (+1 more); longest tracker lifetime 399 days, 8 trackers persist beyond 180 days; cookie banner present; privacy/cookie policy linked; collects personal data via 1 web form(s) (email) with no consent checkbox and no privacy link on the form submitted to third-party host(s): admin.hk.co.",
               DownloadReportUrl="https://reports.consiva.tech/r/8e26f23a0feafc68e46b3e6de679e930.pdf"),
    3776: dict(GapScore=40, TrackersPreConsent=4, ThirdPartiesCount=0, CookieBanner="none",
               CollectsViaForm="name — no consent checkbox",
               ConsentFinding="No consent management platform detected; 4 of 6 cookies are tracking cookies set before consent: _gid, _gat_gtag_UA_112430567_1, _ga_YSP0F4MYLK, _ga; longest tracker lifetime 399 days, 2 trackers persist beyond 180 days; no cookie banner; privacy/cookie policy linked; collects personal data via 1 web form(s) (name) with no consent checkbox.",
               DownloadReportUrl="https://reports.consiva.tech/r/4f359c0d5a5a6a6d067d81d6be0e0c2b.pdf"),
    3698: dict(GapScore=70, TrackersPreConsent=7, ThirdPartiesCount=1, CookieBanner="present",
               CollectsViaForm=None,
               ConsentFinding="CMP detected: OneTrust; 7 of 20 cookies are tracking cookies set before consent: __Secure-YNID, YSC, __Secure-ROLLOUT_TOKEN, VISITOR_INFO1_LIVE, VISITOR_PRIVACY_METADATA, _ga (+1 more); data sent to 1 third-party domains before consent: youtube.com; longest tracker lifetime 399 days, 2 trackers persist beyond 180 days; cookie banner present; privacy/cookie policy linked.",
               DownloadReportUrl="https://reports.consiva.tech/r/dcfa17f8cfa20d93961e58ce825915fd.pdf"),
}
LEGACY_NO_GAP = {   # rows 3766 and 3770, ReportStatus no_gap
    3766: dict(GapScore=100, TrackersPreConsent=0, ThirdPartiesCount=0, CookieBanner="none", CollectsViaForm=None,
               ConsentFinding="No consent management platform detected; no tracking cookies set before consent; no cookie banner; no privacy policy linked.",
               DownloadReportUrl=None),
    3770: dict(GapScore=100, TrackersPreConsent=0, ThirdPartiesCount=0, CookieBanner="none", CollectsViaForm=None,
               ConsentFinding="No consent management platform detected; no tracking cookies set before consent; no cookie banner; privacy/cookie policy linked.",
               DownloadReportUrl=None),
}


class CrmReportColumnFormats(unittest.TestCase):
    """Each format check accepts what the legacy rows hold and refuses what they never hold."""

    def check(self, **cols):
        crm_write._check_columns(cols)

    def test_every_legacy_generated_value_is_accepted(self):
        for row_id, cols in LEGACY.items():
            self.check(**cols)

    def test_every_legacy_no_gap_value_is_accepted(self):
        for row_id, cols in LEGACY_NO_GAP.items():
            self.check(**cols)

    def test_the_allowed_report_columns_are_exactly_the_nine_the_old_job_wrote(self):
        self.assertEqual(set(crm_write.REPORT_COLUMNS), {
            "GapScore", "TrackersPreConsent", "ThirdPartiesCount", "CookieBanner", "CollectsViaForm",
            "ConsentFinding", "UpdatedAt", "DownloadReportUrl", "ReportGeneratedAt"})

    def test_score_is_an_integer_from_0_to_100(self):
        for ok in (0, 1, 50, 100):
            self.check(GapScore=ok)
        for bad in (-1, 101, 55.5, "55", None, True):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=repr(bad)):
                self.check(GapScore=bad)

    def test_counts_are_non_negative_integers(self):
        for col in ("TrackersPreConsent", "ThirdPartiesCount"):
            self.check(**{col: 0})
            for bad in (-1, 1.5, "3", None):
                with self.assertRaises(crm_write.CrmWriteRefused, msg=f"{col}={bad!r}"):
                    self.check(**{col: bad})

    def test_cookie_banner_is_present_or_none(self):
        for ok in ("present", "none"):
            self.check(CookieBanner=ok)
        for bad in ("Present", "yes", "", None, True):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=repr(bad)):
                self.check(CookieBanner=bad)

    def test_collects_via_form_is_null_or_the_legacy_sentence(self):
        self.check(CollectsViaForm=None)
        self.check(CollectsViaForm="email, message, name, phone — no consent checkbox")
        self.check(CollectsViaForm="name — consent checkbox present")
        for bad in ("", "name", "name - no consent checkbox", "x" * 2000, 5):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=repr(bad)[:40]):
                self.check(CollectsViaForm=bad)

    def test_consent_finding_is_a_sentence_ending_in_a_full_stop(self):
        for bad in ("", "no full stop", None, "two\nlines.", "x" * 5000 + "."):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=repr(bad)[:40]):
                self.check(ConsentFinding=bad)

    def test_the_url_must_be_on_the_report_host_with_the_legacy_path(self):
        self.check(DownloadReportUrl=GOOD_URL)
        self.check(DownloadReportUrl=None)
        for bad in ("https://evil.example.com/r/0123456789abcdef0123456789abcdef.pdf",
                    config.REPORT_PUBLIC_BASE_URL + "/r/short.pdf",
                    config.REPORT_PUBLIC_BASE_URL + "/x/0123456789abcdef0123456789abcdef.pdf",
                    config.REPORT_PUBLIC_BASE_URL + "/r/0123456789ABCDEF0123456789ABCDEF.pdf",
                    "http://reports.consiva.tech/r/0123456789abcdef0123456789abcdef.pdf",
                    GOOD_URL + "?x=1", "javascript:alert(1)", ""):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=bad):
                self.check(DownloadReportUrl=bad)

    def test_the_two_timestamps_are_server_time_only(self):
        for col in ("UpdatedAt", "ReportGeneratedAt"):
            self.check(**{col: crm_write.SERVER_NOW})
            for bad in (datetime.now(), "2026-10-12", None):
                with self.assertRaises(crm_write.CrmWriteRefused, msg=f"{col}={bad!r}"):
                    self.check(**{col: bad})

    def test_unknown_columns_are_still_refused(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.check(GapScoreX=5)

    def test_real_scan_values_have_the_legacy_format(self):
        """crm_values() on real fixture scans passes every format check and looks like a legacy row."""
        import report_build as rb
        for fixture in (fx.GA_ONLY_BANNER, fx.GA_NO_BANNER, fx.ADS_NO_POLICY, fx.CLEAN, fx.FORM_ONLY):
            v = rb.crm_values(fx.scan_data(**fixture))
            self.check(**v)
            self.assertEqual(set(v), set(crm_write.SCAN_COLUMNS))
            self.assertTrue(v["ConsentFinding"].startswith(("No consent management platform detected",
                                                            "CMP detected: ")))
            self.assertIn("cookie banner" if v["CookieBanner"] == "present" else "no cookie banner", v["ConsentFinding"])


class CrmReportWrites(DbCase):
    def setUp(self):
        super().setUp()
        self.add_company(7, "scanned")
        self.conn = FakeCrmConn(sources={7: "PartnerImport"})

    def sent_update(self):
        return [q for q in self.conn.sent if q[0].upper().startswith("UPDATE")][-1]

    def test_the_timestamps_are_stamped_by_the_server_not_by_python(self):
        with crm_with(self.conn):
            crm_write.update_company(7, GapScore=40, UpdatedAt=crm_write.SERVER_NOW,
                                     ReportGeneratedAt=crm_write.SERVER_NOW, ReportStatus="no_gap")
        sql, params = self.sent_update()
        self.assertIn("UpdatedAt = SYSUTCDATETIME()", sql)
        self.assertIn("ReportGeneratedAt = SYSUTCDATETIME()", sql)
        self.assertEqual(params, (40, "no_gap", 7, "PartnerImport"))

    def test_a_null_url_is_written_as_a_parameter_so_it_clears_an_old_link(self):
        with crm_with(self.conn):
            crm_write.update_company(7, DownloadReportUrl=None, ReportStatus="no_gap")
        sql, params = self.sent_update()
        self.assertIn("DownloadReportUrl = %s", sql)
        self.assertEqual(params[0], None)

    def test_a_bad_format_is_refused_before_a_connection_is_opened(self):
        with crm_with(self.conn):
            with self.assertRaises(crm_write.CrmWriteRefused):
                crm_write.update_company(7, DownloadReportUrl="https://evil.example.com/x.pdf")
        self.assertEqual(self.conn.sent, [])

    def test_a_test_company_cannot_write_any_report_column(self):
        self.add_company(8, "scanned", is_test=True)
        self.conn.sources[8] = "PartnerImport"
        with crm_with(self.conn):
            for col, val in (("GapScore", 40), ("ConsentFinding", "x."), ("DownloadReportUrl", GOOD_URL),
                             ("UpdatedAt", crm_write.SERVER_NOW), ("CookieBanner", "none")):
                with self.assertRaises(crm_write.TestCompanyRefused, msg=col):
                    crm_write.update_company(8, **{col: val})
        self.assertEqual(self.conn.sent, [])


# --- stage 4 ----------------------------------------------------------------------------------
class ScanStage(ScanCase):
    def scanner(self, outcomes, calls=None):
        """A fake scanner: outcomes is {company_id: (data, err, seconds)} or a callable."""
        async def scan(company):
            if calls is not None:
                calls.append(company["company_id"])
            out = outcomes(company) if callable(outcomes) else outcomes[company["company_id"]]
            return out
        return scan

    def run_stage(self, scanner, **kw):
        w, patch = writer()
        with patch:
            counts = scan_stage.run(scanner=scanner, **kw)
        return counts, w

    def test_a_successful_scan_moves_to_scanned_and_tells_the_crm_done(self):
        self.add_checked(1)
        counts, w = self.run_stage(self.scanner({1: scan_result(**fx.GA_ONLY_BANNER)}))
        r = self.row(1)
        self.assertEqual(r["status"], "scanned")
        self.assertEqual(w.calls, [(1, {"ScrapeStatus": "done"})])
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(float(r["scan_seconds"]), 1.5)
        self.assertIsNotNone(r["scan_id"])

    def test_the_scan_is_stored_keyed_by_company_id_with_the_crm_values(self):
        self.add_checked(1, domain="acme.example.com")
        self.run_stage(self.scanner({1: scan_result(**fx.GA_ONLY_BANNER)}))
        (scan,) = self.scans(1)
        self.assertEqual(scan["lead_ref"], 1)
        self.assertEqual(scan["requested_domain"], "acme.example.com")
        self.assertEqual(scan["scan_status"], "ok")
        self.assertEqual(scan["readiness_score"], 90)
        self.assertEqual(scan["crm_values"]["GapScore"], 90)
        self.assertEqual(self.row(1)["scan_id"], scan["id"])

    def test_the_scanner_is_given_the_company_name_and_domain(self):
        self.add_checked(1, domain="acme.example.com", name="Acme Pvt Ltd")
        seen = []

        async def scan(company):
            seen.append(company)
            return scan_result(**fx.CLEAN)
        self.run_stage(scan)
        self.assertEqual(seen[0]["domain"], "acme.example.com")
        self.assertEqual(seen[0]["company_name"], "Acme Pvt Ltd")

    def test_unscannable_maps_to_unscannable_with_both_crm_columns(self):
        self.add_checked(1)
        counts, w = self.run_stage(self.scanner({1: scan_result(**fx.UNSCANNABLE_404)}))
        r = self.row(1)
        self.assertEqual((r["status"], r["failure_category"]), ("unscannable", "http_error"))
        self.assertEqual(w.calls, [(1, {"ScrapeStatus": "done", "ReportStatus": "unscannable"})])
        self.assertEqual(counts["unscannable"], 1)
        self.assertEqual(self.scans(1)[0]["scan_status"], "unscannable")

    def test_unscannable_categories(self):
        for i, (fixture, cat) in enumerate(((fx.UNSCANNABLE_BOT, "blocked"), (fx.UNSCANNABLE_PARKED, "other")), start=10):
            self.add_checked(i)
            self.run_stage(self.scanner({i: scan_result(**fixture)}))
            self.assertEqual(self.row(i)["failure_category"], cat)

    def test_an_unscannable_scan_never_writes_the_six_consent_columns(self):
        self.add_checked(1)
        _, w = self.run_stage(self.scanner({1: scan_result(**fx.UNSCANNABLE_404)}))
        for _, cols in w.calls:
            self.assertFalse(set(cols) & set(crm_write.SCAN_COLUMNS))

    def test_a_retryable_failure_waits_with_backoff_then_gives_up_after_two_retries(self):
        self.add_checked(1)
        fail = {1: (None, "Page.goto: Timeout 25000ms exceeded", 25.0)}
        waits = []
        for attempt in (1, 2):
            counts, w = self.run_stage(self.scanner(fail))
            r = self.row(1)
            self.assertEqual((r["status"], r["scan_attempts"], r["failure_category"]), ("checked", attempt, "timeout"))
            self.assertEqual(w.calls, [], "no CRM write for a retry")
            waits.append((r["next_retry_at"] - datetime.now(timezone.utc)).total_seconds() / 60)
            self.assertEqual(counts["retry"], 1)
            with db.get_cursor() as cur:
                cur.execute("UPDATE ae_companies SET next_retry_at = NOW() - INTERVAL '1 minute'")
        self.assertLess(abs(waits[0] - settings.SCAN_RETRY_BACKOFF_MINUTES[0]), 1.5)
        self.assertLess(abs(waits[1] - settings.SCAN_RETRY_BACKOFF_MINUTES[1]), 1.5)
        self.assertGreater(waits[1], waits[0], "backoff grows")
        counts, w = self.run_stage(self.scanner(fail))
        self.assertEqual(self.row(1)["status"], "scan_failed")
        self.assertEqual(self.row(1)["scan_attempts"], 3)
        self.assertEqual(w.calls, [(1, {"ScrapeStatus": "failed"})])
        self.assertEqual(counts["scan_failed"], 1)

    def test_a_company_waiting_for_its_retry_is_left_alone(self):
        self.add_checked(1, next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=30))
        calls = []
        self.run_stage(self.scanner({1: scan_result(**fx.CLEAN)}, calls))
        self.assertEqual(calls, [])
        self.assertEqual(self.row(1)["status"], "checked")

    def test_the_failure_category_is_recorded_for_every_kind(self):
        errors = {"net::ERR_NAME_NOT_RESOLVED": "dns", "Timeout 25000ms exceeded": "timeout",
                  "blocked by Cloudflare": "blocked", "server answered HTTP 503": "http_error", "weird": "other"}
        for i, (err, cat) in enumerate(errors.items(), start=20):
            self.add_checked(i)
            self.run_stage(self.scanner({i: (None, err, 2.0)}))
            self.assertEqual(self.row(i)["failure_category"], cat, err)
            self.assertEqual(len(self.scans(i)), 1)
            self.assertEqual(self.scans(i)[0]["scan_status"], "failed")

    def test_companies_still_without_a_domain_are_not_scanned(self):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, status, domain, domain_source)
                           VALUES (1, 'checked', NULL, NULL), (2, 'checked', 'x.example.com', NULL)""")
        calls = []
        self.run_stage(self.scanner(lambda c: scan_result(**fx.CLEAN), calls))
        self.assertEqual(calls, [])

    def test_a_scan_younger_than_14_days_is_reused_not_repeated(self):
        self.add_checked(1, domain="c1.example.com")
        sid = self.seed_scan(1, domain="c1.example.com", age_days=5, **fx.GA_ONLY_BANNER)
        calls = []
        counts, w = self.run_stage(self.scanner({1: scan_result(**fx.CLEAN)}, calls))
        self.assertEqual(calls, [])
        r = self.row(1)
        self.assertEqual((r["status"], r["scan_id"]), ("scanned", sid))
        self.assertEqual(counts["reused"], 1)
        self.assertEqual(w.calls, [(1, {"ScrapeStatus": "done"})])
        self.assertEqual(len(self.scans(1)), 1, "no new scan row")

    def test_a_scan_older_than_14_days_is_repeated(self):
        self.add_checked(1, domain="c1.example.com")
        self.seed_scan(1, domain="c1.example.com", age_days=15, **fx.GA_ONLY_BANNER)
        calls = []
        self.run_stage(self.scanner({1: scan_result(**fx.CLEAN)}, calls))
        self.assertEqual(calls, [1])
        self.assertEqual(len(self.scans(1)), 2)

    def test_a_scan_of_a_different_domain_is_not_reused(self):
        self.add_checked(1, domain="new.example.com")
        self.seed_scan(1, domain="old.example.com", age_days=1, **fx.GA_ONLY_BANNER)
        calls = []
        self.run_stage(self.scanner({1: scan_result(**fx.CLEAN)}, calls))
        self.assertEqual(calls, [1])

    def test_a_reusable_unscannable_scan_is_reused_too(self):
        self.add_checked(1, domain="c1.example.com")
        self.seed_scan(1, domain="c1.example.com", age_days=2, **fx.UNSCANNABLE_404)
        calls = []
        self.run_stage(self.scanner({}, calls))
        self.assertEqual(calls, [])
        self.assertEqual(self.row(1)["status"], "unscannable")

    def test_a_failed_scan_is_never_reused(self):
        self.add_checked(1, domain="c1.example.com")
        company = {"company_id": 1, "company_name": "x", "domain": "c1.example.com"}
        db.insert_report_scan(**scan_adapter.scan_row_from(company, None, "timeout", attempts=1))
        calls = []
        self.run_stage(self.scanner({1: scan_result(**fx.CLEAN)}, calls))
        self.assertEqual(calls, [1])

    def test_test_companies_are_scanned_but_nothing_goes_to_the_crm(self):
        self.add_checked(1, is_test=True)
        self.add_checked(2, is_test=True)
        counts, w = self.run_stage(self.scanner({1: scan_result(**fx.GA_ONLY_BANNER),
                                                 2: scan_result(**fx.UNSCANNABLE_404)}), only_test=True)
        self.assertEqual((self.row(1)["status"], self.row(2)["status"]), ("scanned", "unscannable"))
        self.assertEqual(w.calls, [])

    def test_only_test_and_only_production_filters(self):
        self.add_checked(1, is_test=True)
        self.add_checked(2, is_test=False)
        calls = []
        self.run_stage(self.scanner(lambda c: scan_result(**fx.CLEAN), calls), only_test=True)
        self.assertEqual(calls, [1])

    def test_a_crm_write_failure_keeps_the_company_retryable_and_keeps_the_scan(self):
        self.add_checked(1)
        w, patch = writer(fail=crm_write.CrmWriteFailed("connection dropped"))
        with patch:
            counts = scan_stage.run(scanner=self.scanner({1: scan_result(**fx.GA_ONLY_BANNER)}))
        self.assertEqual(self.row(1)["status"], "checked")
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(len(self.scans(1)), 1, "the scan is kept so the retry can reuse it")
        calls = []
        self.run_stage(self.scanner({1: scan_result(**fx.GA_ONLY_BANNER)}, calls))
        self.assertEqual(calls, [], "the retry reuses the stored scan")
        self.assertEqual(self.row(1)["status"], "scanned")

    def test_a_scan_whose_score_disagrees_with_the_crm_value_is_a_failure(self):
        self.add_checked(1)
        bad = fx.scan_data(**fx.GA_ONLY_BANNER)
        bad["consent_gap_score"] = 12
        counts, w = self.run_stage(self.scanner({1: (bad, None, 1.0)}))
        self.assertEqual(self.row(1)["status"], "checked")
        self.assertEqual(self.row(1)["scan_attempts"], 1)
        self.assertEqual(self.scans(1)[0]["scan_status"], "failed")
        self.assertIn("!= CRM GapScore", self.scans(1)[0]["error"])

    def test_concurrency_is_bounded_by_the_setting(self):
        for i in range(1, 13):
            self.add_checked(i)
        live, peak = [0], [0]

        async def scan(company):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            await asyncio.sleep(0.05)
            live[0] -= 1
            return scan_result(**fx.CLEAN)
        self.run_stage(scan, concurrency=3)
        self.assertEqual(peak[0], 3)
        self.assertEqual(self.count("ae_companies", "status = 'scanned'"), 12)

    def test_work_per_run_is_bounded(self):
        for i in range(1, 11):
            self.add_checked(i)
        calls = []
        counts, _ = self.run_stage(self.scanner(lambda c: scan_result(**fx.CLEAN), calls), limit=4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.count("ae_companies", "status = 'scanned'"), 4)

    def test_the_default_bound_comes_from_settings(self):
        for i in range(1, 6):
            self.add_checked(i)
        calls = []
        with mock.patch.object(settings, "SCANS_PER_RUN", 2):
            self.run_stage(self.scanner(lambda c: scan_result(**fx.CLEAN), calls))
        self.assertEqual(len(calls), 2)

    def test_dry_run_scans_nothing_and_writes_nothing(self):
        self.add_checked(1)
        self.add_checked(2, domain="c2.example.com")
        self.seed_scan(2, domain="c2.example.com", age_days=3, **fx.CLEAN)
        calls = []
        counts, w = self.run_stage(self.scanner({}, calls), dry_run=True)
        self.assertEqual(calls, [])
        self.assertEqual((counts["would_scan"], counts["would_reuse"]), (1, 1))
        self.assertEqual(self.count("ae_companies", "status = 'checked'"), 2)
        self.assertEqual(len(self.scans()), 1)
        self.assertEqual(w.calls, [])

    def test_a_rerun_picks_up_exactly_where_it_left_off(self):
        for i in range(1, 4):
            self.add_checked(i)
        self.run_stage(self.scanner(lambda c: scan_result(**fx.CLEAN)), limit=2)
        calls = []
        self.run_stage(self.scanner(lambda c: scan_result(**fx.CLEAN), calls))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.count("ae_companies", "status = 'scanned'"), 3)

    def test_seconds_per_company_are_reported(self):
        self.add_checked(1)
        counts, _ = self.run_stage(self.scanner({1: (fx.scan_data(**fx.CLEAN), None, 7.25)}))
        self.assertEqual(counts["seconds"], {1: 7.25})


# --- stage 5 ----------------------------------------------------------------------------------
class GateStage(ScanCase):
    def scanned(self, company_id, fixture, is_test=False, age_days=0):
        self.add_company(company_id, "scanned", is_test=is_test)
        sid = self.seed_scan(company_id, age_days=age_days, **fixture)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET scan_id = %s WHERE company_id = %s", (sid, company_id))
        return sid

    def run_gate(self, **kw):
        w, patch = writer()
        with patch:
            counts = gate.run(**kw)
        return counts, w

    def test_a_pass_stores_score_gap_count_band_and_scan_date(self):
        sid = self.scanned(1, fx.GA_NO_BANNER)
        self.run_gate()
        r = self.row(1)
        self.assertEqual((r["status"], r["score"], r["gap_count"], r["band"]), ("gate_passed", 64, 2, "C"))
        with db.get_cursor() as cur:
            cur.execute("SELECT scanned_at FROM report_scans WHERE id = %s", (sid,))
            self.assertEqual(r["report_date"], cur.fetchone()["scanned_at"])

    def test_the_boundary_scans_through_the_real_scorer(self):
        for cid, fixture, want in ((1, fx.GA_ONLY_BANNER, ("gate_passed", 90, "D")),
                                   (2, fx.GA_ONE_COOKIE, ("score_too_high", 96, None)),
                                   (3, fx.CLEAN, ("no_gaps", 100, None)),
                                   (4, fx.FORM_ONLY, ("no_gaps", 75, None)),
                                   (5, fx.ADS_NO_BANNER, ("gate_passed", 46, "B")),
                                   (6, fx.ADS_NO_POLICY, ("gate_passed", 30, "B"))):
            self.scanned(cid, fixture)
        self.run_gate()
        for cid, (st, score, band) in ((1, ("gate_passed", 90, "D")), (2, ("score_too_high", 96, None)),
                                       (3, ("no_gaps", 100, None)), (4, ("no_gaps", 75, None)),
                                       (5, ("gate_passed", 46, "B")), (6, ("gate_passed", 30, "B"))):
            r = self.row(cid)
            self.assertEqual((r["status"], r["score"], r["band"]), (st, score, band), cid)

    def test_a_pass_writes_the_six_consent_columns_and_updated_at_but_no_report_status(self):
        self.scanned(1, fx.GA_NO_BANNER)
        _, w = self.run_gate()
        (cid, cols), = w.calls
        self.assertEqual(cid, 1)
        self.assertEqual(set(cols), set(crm_write.SCAN_COLUMNS) | {"UpdatedAt"})
        self.assertIs(cols["UpdatedAt"], crm_write.SERVER_NOW)
        self.assertEqual(cols["GapScore"], 64)
        self.assertNotIn("ReportStatus", cols)
        self.assertNotIn("DownloadReportUrl", cols)

    def test_the_values_written_are_the_ones_stored_at_scan_time(self):
        self.scanned(1, fx.GA_NO_BANNER)
        with db.get_cursor() as cur:
            cur.execute("SELECT crm_values FROM report_scans")
            stored = cur.fetchone()["crm_values"]
        _, w = self.run_gate()
        self.assertEqual({k: v for k, v in w.calls[0][1].items() if k != "UpdatedAt"}, stored)

    def test_no_gap_gets_the_same_columns_as_the_legacy_no_gap_rows(self):
        self.scanned(1, fx.CLEAN)
        _, w = self.run_gate()
        (_, cols), = w.calls
        # legacy no_gap rows: six consent columns, UpdatedAt, ReportGeneratedAt, ReportStatus, URL NULL
        self.assertEqual(set(cols) | {"ReportStatus"},
                         set(crm_write.SCAN_COLUMNS) | {"UpdatedAt", "ReportGeneratedAt", "DownloadReportUrl", "ReportStatus"})
        self.assertIsNone(cols["DownloadReportUrl"])
        self.assertIs(cols["ReportGeneratedAt"], crm_write.SERVER_NOW)
        self.assertEqual(cols["GapScore"], 100)

    def test_no_gap_end_to_end_through_the_real_write_function_matches_legacy_shape(self):
        self.scanned(1, fx.CLEAN)
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            gate.run()
        sql, params = [q for q in conn.sent if q[0].upper().startswith("UPDATE")][-1]
        for col in ("GapScore", "TrackersPreConsent", "ThirdPartiesCount", "CookieBanner", "CollectsViaForm",
                    "ConsentFinding", "DownloadReportUrl", "ReportStatus"):
            self.assertIn(f"{col} = %s", sql)
        self.assertIn("UpdatedAt = SYSUTCDATETIME()", sql)
        self.assertIn("ReportGeneratedAt = SYSUTCDATETIME()", sql)
        self.assertEqual(params[-2:], (1, "PartnerImport"))
        self.assertIn("no_gap", params)
        self.assertIn(None, params, "the URL is cleared")

    def test_score_too_high_writes_findings_but_no_status_and_no_report(self):
        self.scanned(1, fx.GA_ONE_COOKIE)
        _, w = self.run_gate()
        (_, cols), = w.calls
        self.assertEqual(set(cols), set(crm_write.SCAN_COLUMNS) | {"UpdatedAt"})
        self.assertEqual(self.row(1)["status"], "score_too_high")

    def test_findings_are_stored_once_per_scan(self):
        sid = self.scanned(1, fx.GA_NO_BANNER)
        self.run_gate()
        self.assertEqual(self.count("report_findings", f"scan_id = {sid}"), 2)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET status = 'scanned'")
        self.run_gate()
        self.assertEqual(self.count("report_findings", f"scan_id = {sid}"), 2)

    def test_test_companies_pass_the_gate_without_any_crm_write(self):
        self.scanned(1, fx.GA_NO_BANNER, is_test=True)
        self.scanned(2, fx.CLEAN, is_test=True)
        _, w = self.run_gate(only_test=True)
        self.assertEqual((self.row(1)["status"], self.row(2)["status"]), ("gate_passed", "no_gaps"))
        self.assertEqual(w.calls, [])

    def test_a_crm_failure_leaves_the_company_scanned_and_the_others_proceed(self):
        self.scanned(1, fx.GA_NO_BANNER)
        self.scanned(2, fx.ADS_NO_BANNER)
        calls = []

        def flaky(company_id, **cols):
            calls.append(company_id)
            if company_id == 1:
                raise crm_write.CrmWriteFailed("down")
        with mock.patch.object(crm_write, "update_company", flaky):
            counts = gate.run()
        self.assertEqual(self.row(1)["status"], "scanned")
        self.assertIsNone(self.row(1)["score"], "the score columns rolled back with the status")
        self.assertEqual(self.row(2)["status"], "gate_passed")
        self.assertEqual(counts["errors"], 1)

    def test_only_scanned_companies_are_touched(self):
        self.add_company(1, "checked")
        self.add_company(2, "gate_passed")
        _, w = self.run_gate()
        self.assertEqual(w.calls, [])

    def test_dry_run_decides_nothing_but_says_what_would_happen(self):
        self.scanned(1, fx.GA_NO_BANNER)
        self.scanned(2, fx.CLEAN)
        counts, w = self.run_gate(dry_run=True)
        self.assertEqual(self.count("ae_companies", "status = 'scanned'"), 2)
        self.assertEqual(w.calls, [])
        self.assertEqual(counts["would"], {"gate_passed": 1, "no_gaps": 1})
        self.assertEqual(self.count("report_findings"), 0)

    def test_the_decisions_are_counted(self):
        self.scanned(1, fx.GA_ONLY_BANNER)
        self.scanned(2, fx.GA_ONE_COOKIE)
        self.scanned(3, fx.CLEAN)
        counts, _ = self.run_gate()
        self.assertEqual(counts["decisions"], {"gate_passed": 1, "score_too_high": 1, "no_gaps": 1})
        self.assertEqual(counts["bands"], {"D": 1})


# --- stage 6 ----------------------------------------------------------------------------------
class ReportStage(ScanCase):
    PDF = b"%PDF-1.4 fake"

    def passed(self, company_id, fixture=None, is_test=False, band="C"):
        fixture = fixture or fx.GA_NO_BANNER
        self.add_company(company_id, "gate_passed", is_test=is_test)
        sid = self.seed_scan(company_id, **fixture)
        with db.get_cursor() as cur:
            cur.execute("""UPDATE ae_companies SET scan_id = %s, score = 64, gap_count = 2, band = %s,
                           report_date = NOW() WHERE company_id = %s""", (sid, band, company_id))
        return sid

    def fakes(self, pdf=PDF, why=None, url=GOOD_URL):
        log = {"render": [], "upload": []}

        async def renderer(domain, scan_date, scan):
            log["render"].append(domain)
            return (pdf, why, "full") if pdf else (None, why or "content overflows the page", None)

        def uploader(pdf_bytes, domain):
            log["upload"].append(domain)
            return "r/" + url.rsplit("/", 1)[1], url
        return log, renderer, uploader

    def run_report(self, renderer, uploader, **kw):
        w, patch = writer()
        with patch:
            counts = report_stage.run(renderer=renderer, uploader=uploader, **kw)
        return counts, w

    def test_only_gate_passed_companies_get_a_report(self):
        self.passed(1)
        for cid, st in ((2, "scanned"), (3, "no_gaps"), (4, "score_too_high"), (5, "checked")):
            # decoys with a perfectly good stored scan, so a stage that ignored the gate WOULD render them
            self.add_company(cid, st)
            sid = self.seed_scan(cid, **fx.GA_NO_BANNER)
            with db.get_cursor() as cur:
                cur.execute("UPDATE ae_companies SET scan_id = %s, score = 64, gap_count = 2 WHERE company_id = %s",
                            (sid, cid))
        log, renderer, uploader = self.fakes()
        self.run_report(renderer, uploader)
        self.assertEqual(log["render"], ["c1.example.com"])
        self.assertEqual(self.row(1)["status"], "reported")
        for cid, st in ((2, "scanned"), (3, "no_gaps"), (4, "score_too_high"), (5, "checked")):
            self.assertEqual(self.row(cid)["status"], st)

    def test_a_report_stores_the_ref_url_and_date_and_writes_the_crm_columns(self):
        sid = self.passed(1)
        log, renderer, uploader = self.fakes()
        counts, w = self.run_report(renderer, uploader)
        r = self.row(1)
        self.assertEqual(r["report_url"], GOOD_URL)
        self.assertIsNotNone(r["report_ref"])
        self.assertIsNotNone(r["report_date"])
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM reports WHERE id = %s", (r["report_ref"],))
            rep = cur.fetchone()
        self.assertEqual((rep["lead_ref"], rep["scan_id"], rep["status"], rep["public_url"]),
                         (1, sid, "rendered", GOOD_URL))
        self.assertEqual(rep["size_bytes"], len(self.PDF))
        (cid, cols), = w.calls
        self.assertEqual((cid, cols["DownloadReportUrl"]), (1, GOOD_URL))
        self.assertIs(cols["ReportGeneratedAt"], crm_write.SERVER_NOW)
        self.assertEqual(counts["reported"], 1)

    def test_the_crm_status_generated_travels_with_the_url_in_one_write(self):
        self.passed(1)
        _, renderer, uploader = self.fakes()
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            report_stage.run(renderer=renderer, uploader=uploader)
        updates = [q for q in conn.sent if q[0].upper().startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        sql, params = updates[0]
        self.assertIn("DownloadReportUrl = %s", sql)
        self.assertIn("ReportStatus = %s", sql)
        self.assertIn("ReportGeneratedAt = SYSUTCDATETIME()", sql)
        self.assertIn("generated", params)
        self.assertIn(GOOD_URL, params)

    def test_a_url_off_the_report_host_is_refused_and_the_company_stays_put(self):
        self.passed(1)
        _, renderer, uploader = self.fakes(url="https://evil.example.com/r/0123456789abcdef0123456789abcdef.pdf")
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            counts = report_stage.run(renderer=renderer, uploader=uploader)
        self.assertEqual(self.row(1)["status"], "gate_passed")
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(conn.sent, [])

    def test_a_render_failure_is_recorded_and_the_company_can_retry(self):
        self.passed(1)
        log, renderer, uploader = self.fakes(pdf=None, why="content overflows the page")
        counts, w = self.run_report(renderer, uploader)
        self.assertEqual(self.row(1)["status"], "gate_passed")
        self.assertEqual(log["upload"], [])
        self.assertEqual(w.calls, [])
        self.assertEqual(counts["render_failed"], 1)
        with db.get_cursor() as cur:
            cur.execute("SELECT status, error FROM reports")
            self.assertEqual(dict(cur.fetchone()), {"status": "failed", "error": "content overflows the page"})

    def test_an_existing_report_for_the_same_scan_is_reused_not_rebuilt(self):
        self.passed(1)
        log, renderer, uploader = self.fakes()
        self.run_report(renderer, uploader)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET status = 'gate_passed', report_ref = NULL, report_url = NULL")
        log2, renderer2, uploader2 = self.fakes(url=URL.format("f" * 32))
        counts, w = self.run_report(renderer2, uploader2)
        self.assertEqual((log2["render"], log2["upload"]), ([], []))
        self.assertEqual(self.row(1)["report_url"], GOOD_URL)
        self.assertEqual(counts["reused"], 1)

    def test_a_rescan_overwrites_the_crm_report_columns_and_the_old_report_is_superseded(self):
        self.passed(1)
        log, renderer, uploader = self.fakes()
        _, w1 = self.run_report(renderer, uploader)
        new_url = URL.format("a" * 32)
        sid2 = self.seed_scan(1, **fx.ADS_NO_BANNER)
        with db.get_cursor() as cur:
            cur.execute("""UPDATE ae_companies SET status = 'gate_passed', scan_id = %s, score = 46, gap_count = 3,
                           band = 'B' WHERE company_id = 1""", (sid2,))
        log2, renderer2, uploader2 = self.fakes(url=new_url)
        _, w2 = self.run_report(renderer2, uploader2)
        self.assertEqual(w1.calls[0][1]["DownloadReportUrl"], GOOD_URL)
        self.assertEqual(w2.calls[0][1]["DownloadReportUrl"], new_url, "the new value overwrites the old")
        self.assertEqual(self.row(1)["report_url"], new_url)
        with db.get_cursor() as cur:
            cur.execute("SELECT status, public_url FROM reports ORDER BY id")
            self.assertEqual([tuple(r.values()) for r in cur.fetchall()],
                             [("superseded", GOOD_URL), ("rendered", new_url)])

    def test_nothing_is_ever_deleted_from_r2(self):
        src = (ROOT / "campaign" / "report_stage.py").read_text()
        self.assertNotIn("storage.delete", src)
        self.assertNotIn("delete_object", src)
        self.assertNotIn(".delete(", src)

    def test_test_companies_render_and_upload_for_real_but_never_touch_the_crm(self):
        self.passed(1, is_test=True)
        log, renderer, uploader = self.fakes()
        counts, w = self.run_report(renderer, uploader, only_test=True)
        self.assertEqual(log["render"], ["c1.example.com"])
        self.assertEqual(len(log["upload"]), 1)
        self.assertEqual(w.calls, [])
        self.assertEqual(self.row(1)["status"], "reported")
        self.assertEqual(self.row(1)["report_url"], GOOD_URL)

    def test_a_pushed_report_is_marked_pushed_only_for_production(self):
        self.passed(1)
        self.passed(2, is_test=True)
        _, renderer, uploader = self.fakes()
        self.run_report(renderer, uploader)
        with db.get_cursor() as cur:
            cur.execute("SELECT lead_ref, crm_pushed_at IS NOT NULL AS pushed FROM reports ORDER BY lead_ref")
            self.assertEqual([(r["lead_ref"], r["pushed"]) for r in cur.fetchall()], [(1, True), (2, False)])

    def test_dry_run_renders_nothing_uploads_nothing_writes_nothing(self):
        self.passed(1)
        log, renderer, uploader = self.fakes()
        counts, w = self.run_report(renderer, uploader, dry_run=True)
        self.assertEqual((log["render"], log["upload"], w.calls), ([], [], []))
        self.assertEqual(self.count("reports"), 0)
        self.assertEqual(self.row(1)["status"], "gate_passed")
        self.assertEqual(counts["would_report"], 1)

    def test_work_per_run_is_bounded(self):
        for i in range(1, 6):
            self.passed(i)
        log, renderer, uploader = self.fakes()
        self.run_report(renderer, uploader, limit=2)
        self.assertEqual(len(log["render"]), 2)


class StagesAreWired(unittest.TestCase):
    def test_stages_3_to_6_are_real_in_the_stage_table(self):
        by_name = {s.name: s for s in stages.STAGES}
        for name in ("scan", "score_gate", "report"):
            self.assertTrue(by_name[name].implemented, name)

    def test_every_declared_transition_is_allowed_by_the_model(self):
        for stage in stages.STAGES:
            for src, dst in stage.transitions:
                self.assertTrue(status.is_allowed(src, dst), f"stage {stage.number}: {src} -> {dst}")


if __name__ == "__main__":
    unittest.main()
