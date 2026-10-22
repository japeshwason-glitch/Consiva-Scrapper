"""Adopting leads that already have a report, and writing a draft into the CRM AE_ columns."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import adopt, crm_write, draft_templates, draft_writer, settings
from tests import campaign_fixtures as fx
from tests.test_campaign import DbCase, FakeCrm, FakeCrmConn, crm_with, writer

URL = "https://reports.consiva.tech/r/0123456789abcdef0123456789abcdef.pdf"


class AdoptCrm(FakeCrm):
    def report_rows(self, ids):
        return [{"id": i, "website": c["website"], "company_name": c["company_name"], "company_scale": c["company_scale"],
                 "industry": c["industry"], "gap_score": c.get("gap_score"), "report_url": c.get("report_url"),
                 "report_status": c["report_status"], "report_generated": c.get("report_generated")}
                for i, c in self.companies.items() if i in ids]


def ok_mx(domain, timeout):
    return ["mx.example.com"]


def scanner_for(fixture):
    async def scan(company):
        return fx.scan_data(domain=company["domain"], **fixture), None, 2.0
    return scan


class Adopting(DbCase):
    def setUp(self):
        super().setUp()
        self.crm = AdoptCrm()
        self.crm.company(1, website="acme.example.com", report="generated", scale="51-100", industry="Textiles")
        self.crm.companies[1].update(gap_score=46, report_url=URL,
                                     report_generated=datetime.now(timezone.utc) - timedelta(days=5))
        self.crm.contact(1, "boss@acme.example.com", "MD", first_name="Asha")

    def adopt(self, fixture=None, ids=(1,)):
        w, patch = writer()
        with patch:
            out = adopt.adopt(self.crm, ids, scanner=scanner_for(fixture or fx.ADS_NO_BANNER), resolver=ok_mx)
        return out, w

    def test_a_company_with_a_report_is_adopted_at_reported_with_the_crm_report_kept(self):
        out, w = self.adopt()
        self.assertEqual(out[0]["status"], "reported")
        r = self.row(1)
        self.assertEqual((r["status"], r["score"], r["band"], r["report_url"], r["is_test"]), ("reported", 46, "B", URL, False))
        self.assertEqual(r["gap_count"], 3)
        self.assertIsNotNone(r["scan_id"])
        self.assertEqual(self.count("ae_contacts", "company_id = 1 AND first_name = 'Asha'"), 1)

    def test_adopting_writes_nothing_to_the_crm(self):
        _, w = self.adopt()
        self.assertEqual(w.calls, [])

    def test_the_report_date_is_the_crm_report_date(self):
        self.adopt()
        self.assertLess(datetime.now(timezone.utc) - self.row(1)["report_date"], timedelta(days=6))

    def test_a_company_without_a_generated_report_is_refused(self):
        self.crm.companies[1]["report_status"] = "no_gap"
        with self.assertRaises(adopt.AdoptRefused):
            self.adopt()
        self.assertEqual(self.count("ae_companies"), 0)

    def test_a_report_url_off_the_report_host_is_refused(self):
        self.crm.companies[1]["report_url"] = "https://evil.example.com/r/0123456789abcdef0123456789abcdef.pdf"
        with self.assertRaises(adopt.AdoptRefused):
            self.adopt()

    def test_a_score_above_the_ceiling_is_refused(self):
        self.crm.companies[1]["gap_score"] = 95
        with self.assertRaises(adopt.AdoptRefused):
            self.adopt()

    def test_an_unknown_company_is_refused(self):
        with self.assertRaises(adopt.AdoptRefused):
            self.adopt(ids=(1, 99))

    def test_a_company_already_taken_in_is_refused(self):
        self.add_company(1)
        with self.assertRaises(adopt.AdoptRefused):
            self.adopt()

    def test_a_fresh_scan_with_no_gap_is_not_adopted(self):
        out, _ = self.adopt(fx.CLEAN)
        self.assertEqual(self.row(1)["status"], "checked")
        self.assertIn("no gap", out[0]["note"])

    def row(self, cid):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_companies WHERE company_id = %s", (cid,))
            return cur.fetchone()


class AeFormats(unittest.TestCase):
    def ok(self, **kw):
        crm_write._check_columns(kw)

    def test_good_values_pass(self):
        self.ok(AE_DraftBand="B", AE_DraftBody="<p>x</p>", AE_DraftEmail="a@b.co", AE_DraftStatus="ready",
                AE_DraftSubject="A short report", AE_DraftVersion="B-0.1-placeholder", AE_DraftGeneratedAt=crm_write.SERVER_NOW)

    def test_bad_values_are_refused(self):
        for col, bad in (("AE_DraftBand", "E"), ("AE_DraftBand", "AB"), ("AE_DraftStatus", "sent"), ("AE_DraftStatus", "Ready"),
                         ("AE_DraftVersion", "x" * 21), ("AE_DraftSubject", "two\nlines"), ("AE_DraftSubject", "x" * 256),
                         ("AE_DraftEmail", "not-an-email"), ("AE_DraftBody", ""), ("AE_DraftGeneratedAt", datetime.now())):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=f"{col}={bad!r}"):
                self.ok(**{col: bad})

    def test_the_template_label_fits_the_crm_column(self):
        for band in "ABCD":
            self.assertLessEqual(len(draft_templates.crm_version(draft_templates.template_version(band))), 20)
        self.assertEqual(draft_templates.crm_version("B-0.1-placeholder-0a1b2c3d"), "B-0.1-placeholder")


class WritingDrafts(DbCase):
    def drafted(self, cid, is_test=False, status_="ready", version="B-1.0-approved-0a1b2c3d"):
        self.add_company(cid, "drafted", is_test=is_test)
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_drafts (company_id, contact_id, to_email, subject, body_html, band, draft_status, template_version)
                           VALUES (%s, 'c', 'boss@acme.example.com', 'A short consent report', '<p>Hi</p>', 'B', %s, %s)""",
                        (cid, status_, version))

    def test_a_production_draft_is_written_to_exactly_the_ae_columns_and_moves_on(self):
        self.drafted(1)
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            counts = draft_writer.run()
        self.assertEqual(counts["error_detail"], [])
        sql, params = [q for q in conn.sent if q[0].upper().startswith("UPDATE")][-1]
        for col in ("AE_DraftBand", "AE_DraftBody", "AE_DraftEmail", "AE_DraftStatus", "AE_DraftSubject", "AE_DraftVersion"):
            self.assertIn(f"{col} = %s", sql)
        self.assertIn("AE_DraftGeneratedAt = SYSUTCDATETIME()", sql)
        for never in ("AE_DraftContactId", "AE_DraftSentAt", "GapScore", "ReportStatus", "ScrapeStatus"):
            self.assertNotIn(never, sql)
        self.assertIn("B-1.0-approved", params)
        self.assertEqual(self.status_of(1)[0], "written_to_crm")
        self.assertEqual(counts["written"], 1)
        self.assertEqual(self.count("ae_drafts", "written_to_crm_at IS NOT NULL"), 1)

    def test_a_risky_draft_is_written_as_risky(self):
        self.drafted(1, status_="risky")
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            draft_writer.run()
        self.assertIn("risky", [q for q in conn.sent if q[0].upper().startswith("UPDATE")][-1][1])

    def test_a_test_company_is_never_written(self):
        self.drafted(1, is_test=True)
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            counts = draft_writer.run()
        self.assertEqual(conn.sent, [])
        self.assertEqual((self.status_of(1)[0], counts["claimed"]), ("drafted", 0))

    def test_a_crm_failure_leaves_the_company_drafted(self):
        self.drafted(1)
        w, patch = writer(fail=crm_write.CrmWriteFailed("down"))
        with patch:
            counts = draft_writer.run()
        self.assertEqual((self.status_of(1)[0], counts["errors"]), ("drafted", 1))

    def test_dry_run_writes_nothing(self):
        self.drafted(1)
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn):
            counts = draft_writer.run(dry_run=True)
        self.assertEqual((conn.sent, counts["would_write"], self.status_of(1)[0]), ([], 1, "drafted"))


if __name__ == "__main__":
    unittest.main()
