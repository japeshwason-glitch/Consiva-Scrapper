"""
Safeguards added after the 3795/3812 incident, and the placeholder-draft rule:
  * crm_write refuses every CRM write unless the connected Postgres database is the configured primary;
  * an HTTP 403 on the homepage is a retryable 'blocked' failure, never 'unscannable';
  * a placeholder template's draft never reaches the CRM as ready/risky (only as 'expired', and only if allowed);
  * AE_DraftContactId stays empty and nothing depends on it.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import crm_write, draft_writer, scan_adapter, scan_stage, settings, status
from tests import campaign_fixtures as fx
from tests.test_campaign import DbCase, FakeCrmConn, crm_with, writer
from tests.test_campaign_scan import ScanCase, scan_result


class PrimaryDatabaseGuard(DbCase):
    """A throwaway, test or demo database must never be able to drive a real CRM write."""

    def setUp(self):
        super().setUp()
        self.add_company(7, "scanned")
        self.conn = FakeCrmConn(sources={7: "PartnerImport"})

    def not_primary(self):
        return mock.patch.object(settings, "PRIMARY_DATABASE", "consiva")      # we are connected to ae_test_xxxx

    def test_the_default_primary_is_consiva(self):
        import importlib
        import campaign.settings as s
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AE_PRIMARY_DATABASE", None)
            self.assertEqual(importlib.reload(s).PRIMARY_DATABASE, "consiva")
        importlib.reload(s)

    def test_a_non_primary_database_cannot_write_any_column(self):
        cases = ({"ScrapeStatus": "done"}, {"ReportStatus": "no_gap"}, {"GapScore": 40},
                 {"DownloadReportUrl": None}, {"AE_DraftBand": "B"})
        with self.not_primary(), crm_with(self.conn):
            for cols in cases:
                with self.assertRaises(crm_write.CrmWriteRefused, msg=str(cols)):
                    crm_write.update_company(7, **cols)
        self.assertEqual(self.conn.sent, [], "no statement was sent to the CRM")

    def test_no_crm_connection_is_even_opened(self):
        opened = []
        with self.not_primary(), mock.patch.object(crm_write, "_connect", lambda: opened.append(1)):
            with self.assertRaises(crm_write.CrmWriteRefused):
                crm_write.update_company(7, ScrapeStatus="done")
        self.assertEqual(opened, [])

    def test_the_error_names_both_databases(self):
        with self.not_primary():
            with self.assertRaises(crm_write.CrmWriteRefused) as cm:
                crm_write.update_company(7, ScrapeStatus="done")
        msg = str(cm.exception)
        self.assertIn("consiva", msg)
        self.assertIn(self._name, msg)

    def test_the_primary_database_may_write(self):
        with mock.patch.object(settings, "PRIMARY_DATABASE", self._name), crm_with(self.conn):
            crm_write.update_company(7, ScrapeStatus="done")
        self.assertEqual(len([q for q in self.conn.sent if q[0].upper().startswith("UPDATE")]), 1)

    def test_a_status_change_that_needs_a_crm_write_is_undone_when_the_database_is_not_primary(self):
        self.add_company(8, "checked")
        with self.not_primary(), crm_with(self.conn):
            with self.assertRaises(crm_write.CrmWriteRefused):
                with db.get_cursor() as cur:
                    status.set_status(cur, 8, "scanned", "scan ok")
        self.assertEqual(self.status_of(8)[0], "checked")
        self.assertEqual(self.conn.sent, [])

    def test_it_is_checked_against_the_database_actually_connected_not_the_url_setting(self):
        """Pointing DATABASE_URL's text at 'consiva' would not help: the check asks Postgres which database it is."""
        import config
        orig = config.DATABASE_URL
        try:
            with self.not_primary(), crm_with(self.conn):
                self.assertIn(self._name, config.DATABASE_URL)
                with self.assertRaises(crm_write.CrmWriteRefused):
                    crm_write.update_company(7, ScrapeStatus="done")
        finally:
            config.DATABASE_URL = orig

    def test_test_companies_are_still_refused_first_on_the_primary(self):
        self.add_company(9, "scanned", is_test=True)
        with mock.patch.object(settings, "PRIMARY_DATABASE", self._name), crm_with(self.conn):
            with self.assertRaises(crm_write.TestCompanyRefused):
                crm_write.update_company(9, ScrapeStatus="done")


class Http403IsBlockedAndRetryable(ScanCase):
    def run_stage(self, outcomes, **kw):
        async def scan(company):
            return outcomes[company["company_id"]]
        w, patch = writer()
        with patch:
            counts = scan_stage.run(scanner=scan, **kw)
        return counts, w

    def test_a_403_is_a_blocked_failure_not_unscannable(self):
        self.add_checked(1)
        counts, w = self.run_stage({1: scan_result(**fx.UNSCANNABLE_403)})
        r = self.row(1)
        self.assertEqual((r["status"], r["failure_category"], r["scan_attempts"]), ("checked", "blocked", 1))
        self.assertIsNotNone(r["next_retry_at"])
        self.assertEqual(w.calls, [], "a retry writes nothing to the CRM, and never ReportStatus 'unscannable'")
        self.assertEqual((counts["retry"], counts["unscannable"]), (1, 0))
        self.assertEqual(counts["failure_categories"], {"blocked": 1})

    def test_after_two_retries_it_is_scan_failed_and_the_crm_hears_failed(self):
        self.add_checked(1)
        w_all = []
        for attempt in (1, 2, 3):
            counts, w = self.run_stage({1: scan_result(**fx.UNSCANNABLE_403)})
            w_all += w.calls
            with db.get_cursor() as cur:
                cur.execute("UPDATE ae_companies SET next_retry_at = NOW() - INTERVAL '1 minute'")
        self.assertEqual(self.row(1)["status"], "scan_failed")
        self.assertEqual(self.row(1)["scan_attempts"], 3)
        self.assertEqual(w_all, [(1, {"ScrapeStatus": "failed"})])

    def test_a_stored_403_scan_is_never_reused(self):
        self.add_checked(1, domain="c1.example.com")
        self.seed_scan(1, domain="c1.example.com", age_days=1, **fx.UNSCANNABLE_403)
        calls = []

        async def scan(company):
            calls.append(1)
            return scan_result(**fx.CLEAN)
        w, patch = writer()
        with patch:
            scan_stage.run(scanner=scan)
        self.assertEqual(calls, [1])
        self.assertEqual(self.row(1)["status"], "scanned")

    def test_other_http_errors_stay_unscannable(self):
        self.add_checked(1)
        self.run_stage({1: scan_result(**fx.UNSCANNABLE_404)})
        self.assertEqual(self.row(1)["status"], "unscannable")

    def test_a_bot_check_page_stays_unscannable(self):
        self.add_checked(1)
        self.run_stage({1: scan_result(**fx.UNSCANNABLE_BOT)})
        self.assertEqual(self.row(1)["status"], "unscannable")

    def test_the_scan_itself_is_still_stored(self):
        self.add_checked(1)
        self.run_stage({1: scan_result(**fx.UNSCANNABLE_403)})
        self.assertEqual(self.scans(1)[0]["http_status"], 403)

    def test_test_companies_follow_the_same_rule_without_any_crm_write(self):
        self.add_checked(1, is_test=True)
        counts, w = self.run_stage({1: scan_result(**fx.UNSCANNABLE_403)}, only_test=True)
        self.assertEqual((self.row(1)["status"], self.row(1)["failure_category"]), ("checked", "blocked"))
        self.assertEqual(w.calls, [])

    def test_the_403_text_classifies_as_blocked(self):
        self.assertEqual(scan_adapter.classify_failure("homepage returned HTTP 403"), "blocked")


class PlaceholderDraftsNeverReachTheCrmAsSendable(DbCase):
    PLACEHOLDER = "B-0.1-placeholder-0a1b2c3d"

    def drafted(self, cid, version=None, status_="ready", is_test=False):
        self.add_company(cid, "drafted", is_test=is_test)
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_drafts (company_id, contact_id, to_email, subject, body_html, band, draft_status, template_version)
                           VALUES (%s, '1b0f0a5e-0000-4000-8000-000000000001', 'boss@acme.example.com', 'A short consent report',
                                   '<p>Hi</p>', 'B', %s, %s)""", (cid, status_, version or self.PLACEHOLDER))

    def write(self, **kw):
        conn = FakeCrmConn(sources={1: "PartnerImport", 2: "PartnerImport"})
        with crm_with(conn):
            counts = draft_writer.run(**kw)
        return counts, conn

    def sent_update(self, conn):
        return [q for q in conn.sent if q[0].upper().startswith("UPDATE")]

    def test_the_flag_defaults_to_false(self):
        self.assertFalse(settings.ALLOW_PLACEHOLDER_AE_WRITE)

    def test_with_the_flag_off_a_placeholder_draft_is_not_written_at_all(self):
        self.drafted(1)
        counts, conn = self.write()
        self.assertEqual(conn.sent, [])
        self.assertEqual(self.status_of(1)[0], "drafted")
        self.assertEqual(counts["blocked"], {"placeholder_template": 1})
        self.assertEqual(counts["written"], 0)

    def test_with_the_flag_on_it_is_written_as_expired_never_ready_or_risky(self):
        self.drafted(1, status_="ready")
        self.drafted(2, status_="risky")
        with mock.patch.object(settings, "ALLOW_PLACEHOLDER_AE_WRITE", True):
            counts, conn = self.write()
        updates = self.sent_update(conn)
        self.assertEqual(len(updates), 2)
        for sql, params in updates:
            self.assertIn("expired", params)
            self.assertNotIn("ready", params)
            self.assertNotIn("risky", params)
            self.assertTrue(any("placeholder" in str(p) for p in params), "the version still says placeholder")
        self.assertEqual((self.status_of(1)[0], self.status_of(2)[0]), ("written_to_crm", "written_to_crm"))
        self.assertEqual(counts["written"], 2)

    def test_the_internal_draft_status_is_left_as_it_was(self):
        self.drafted(1, status_="risky")
        with mock.patch.object(settings, "ALLOW_PLACEHOLDER_AE_WRITE", True):
            self.write()
        with db.get_cursor() as cur:
            cur.execute("SELECT draft_status FROM ae_drafts WHERE company_id = 1")
            self.assertEqual(cur.fetchone()["draft_status"], "risky")

    def test_an_approved_template_is_written_as_ready_or_risky(self):
        self.drafted(1, version="B-1.0-approved-0a1b2c3d", status_="ready")
        self.drafted(2, version="B-1.0-approved-0a1b2c3d", status_="risky")
        counts, conn = self.write()
        params = [p for _, ps in self.sent_update(conn) for p in ps]
        self.assertIn("ready", params)
        self.assertIn("risky", params)
        self.assertNotIn("expired", params)

    def test_crm_write_itself_refuses_ready_or_risky_with_a_placeholder_version(self):
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        self.add_company(1, "drafted")
        with crm_with(conn):
            for st in ("ready", "risky"):
                with self.assertRaises(crm_write.CrmWriteRefused, msg=st):
                    crm_write.update_company(1, AE_DraftStatus=st, AE_DraftVersion="B-0.1-placeholder")
            with self.assertRaises(crm_write.CrmWriteRefused, msg="no version given: cannot prove it is not a placeholder"):
                crm_write.update_company(1, AE_DraftStatus="ready")
            crm_write.update_company(1, AE_DraftStatus="expired", AE_DraftVersion="B-0.1-placeholder")
            crm_write.update_company(1, AE_DraftStatus="ready", AE_DraftVersion="B-1.0-approved")
        self.assertEqual(len(self.sent_update(conn)), 2)

    def test_expired_is_a_valid_ae_status_and_the_vocabulary_is_closed(self):
        crm_write._check_columns({"AE_DraftStatus": "expired"})
        for bad in ("sent", "Ready", "", None):
            with self.assertRaises(crm_write.CrmWriteRefused):
                crm_write._check_columns({"AE_DraftStatus": bad})

    def test_a_test_company_is_never_written_whatever_the_flag(self):
        self.drafted(1, is_test=True)
        with mock.patch.object(settings, "ALLOW_PLACEHOLDER_AE_WRITE", True):
            counts, conn = self.write()
        self.assertEqual((conn.sent, counts["claimed"]), ([], 0))


class AeDraftContactIdStaysEmpty(DbCase):
    def test_columns_for_never_includes_it_or_the_sent_date(self):
        draft = {"band": "B", "body_html": "<p>x</p>", "to_email": "a@b.co", "draft_status": "ready",
                 "subject": "S", "template_version": "B-1.0-approved-0a1b2c3d"}
        cols = draft_writer.columns_for(draft)
        self.assertNotIn("AE_DraftContactId", cols)
        self.assertNotIn("AE_DraftSentAt", cols)

    def test_a_write_succeeds_with_a_guid_contact_and_sends_no_contact_id_column(self):
        self.add_company(1, "drafted")
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_drafts (company_id, contact_id, to_email, subject, body_html, band, draft_status, template_version)
                           VALUES (1, '1b0f0a5e-0000-4000-8000-000000000001', 'a@b.co', 'S', '<p>x</p>', 'B', 'ready', 'B-1.0-approved-0a1b2c3d')""")
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            counts = draft_writer.run()
        self.assertEqual((counts["written"], counts["errors"]), (1, 0))
        sql = [q for q in conn.sent if q[0].upper().startswith("UPDATE")][-1][0]
        self.assertNotIn("AE_DraftContactId", sql)

    def test_a_draft_without_a_contact_id_value_still_writes(self):
        self.add_company(1, "drafted")
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_drafts (company_id, to_email, subject, body_html, band, draft_status, template_version)
                           VALUES (1, 'a@b.co', 'S', '<p>x</p>', 'B', 'ready', 'B-1.0-approved-0a1b2c3d')""")
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            self.assertEqual(draft_writer.run()["written"], 1)


class OnlyTheNamedCompanies(ScanCase):
    """--ids / --adopt-ids mean "only these": no stage may touch any other company in the database."""

    def two(self, st, **kw):
        for cid in (1, 2):
            self.add_company(cid, st, **kw)

    def test_claim_is_limited_to_the_scope(self):
        self.two("pending")
        with status.only_companies([2]):
            with status.claim("pending", 10) as (cur, rows):
                self.assertEqual([r["company_id"] for r in rows], [2])
        with status.claim("pending", 10) as (cur, rows):
            self.assertEqual(sorted(r["company_id"] for r in rows), [1, 2], "the scope ended with the block")

    def test_the_scope_ends_even_when_the_block_raises(self):
        with self.assertRaises(RuntimeError):
            with status.only_companies([1]):
                raise RuntimeError("boom")
        self.assertEqual(status.scope_sql(), (" AND batch IS NULL", []))     # no id limit; untagged companies only

    def test_no_scope_means_no_id_limit(self):
        self.assertEqual(status.scope_sql(), (" AND batch IS NULL", []))
        with status.only_companies([]):
            self.assertEqual(status.scope_sql(), (" AND batch IS NULL", []))

    def test_the_scan_stage_scans_only_the_named_company(self):
        self.add_checked(1)
        self.add_checked(2)
        seen = []

        async def scan(company):
            seen.append(company["company_id"])
            return scan_result(**fx.CLEAN)
        w, patch = writer()
        with patch, status.only_companies([2]):
            scan_stage.run(scanner=scan)
        self.assertEqual(seen, [2])
        self.assertEqual((self.row(1)["status"], self.row(2)["status"]), ("checked", "scanned"))
        self.assertEqual([c for c, _ in w.calls], [2], "and only the named company reaches the CRM")

    def test_the_report_stage_picks_only_the_named_company(self):
        self.two("gate_passed")
        from campaign import report_stage
        with status.only_companies([1]):
            self.assertEqual([r["company_id"] for r in report_stage._candidates(10, None)], [1])

    def test_the_domain_stage_resolves_only_the_named_company(self):
        for cid in (1, 2):
            self.add_company(cid, "checked", domain=None)
        from campaign import domains
        with status.only_companies([2]):
            with domains._claim_unresolved(10, None) as (cur, rows):
                self.assertEqual([r["company_id"] for r in rows], [2])

    def test_the_verify_dry_run_sees_only_the_named_company(self):
        self.two("contact_ranked")
        from campaign import verify
        with status.only_companies([1]):
            counts = verify.run(dry_run=True)
        self.assertEqual(counts["companies_seen"], 1)

    def test_prepare_with_ids_runs_every_production_stage_inside_the_scope(self):
        from campaign import jobs, stages
        from tests.test_campaign import FakeCrm
        seen = []

        def make(n):
            def fn(**kw):
                seen.append((n, status.scope_sql()[1]))
                return {}
            return stages.Stage(n, f"s{n}", "", (), fn if n != 1 else None)
        table = tuple(make(n) for n in range(1, 11))
        crm = FakeCrm()
        crm.company(5)
        with mock.patch.object(stages, "STAGES", table):
            jobs.prepare(production=True, ids=[5], reader=crm)
        self.assertTrue(seen)
        self.assertTrue(all(scope == [[5]] for _, scope in seen), seen)

    def test_adoption_runs_the_free_checks_and_domain_stage_inside_the_scope(self):
        import re
        src = (ROOT / "campaign" / "adopt.py").read_text()
        self.assertRegex(src, r"with status\.only_companies\(ids\):\s+checks\.run\(")


class PricesAreMarkedUnverified(unittest.TestCase):
    def test_the_price_values_are_config_and_flagged_unverified(self):
        from campaign import settings_drafts as cfg
        self.assertFalse(cfg.PRICES_VERIFIED)
        self.assertIn("UNVERIFIED", (ROOT / "campaign" / "settings_drafts.py").read_text())

    def test_the_cost_estimate_says_so(self):
        from campaign import draft_model
        self.assertFalse(draft_model.estimate_cost_note()["prices_verified"])


if __name__ == "__main__":
    unittest.main()
