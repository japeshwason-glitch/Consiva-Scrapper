"""rewrite_drafts: unsent drafts move to the current template and report host; a sent draft is never written."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
import db

from campaign import crm_write, rewrite_drafts, settings
from tests.test_campaign import DbCase, FakeCrmConn, crm_with

OLD = "https://reports-consiva.gignati.com/r/"
NEW = "https://reports.consiva.tech/r/"
KEY = "0123456789abcdef0123456789abcdef"


class Reader:
    """What crm_read.CrmReader.unsent_drafts returns, fixed."""

    def __init__(self, companies=(), leads=()):
        self.c, self.l = list(companies), list(leads)

    def unsent_drafts(self):
        return {"companies": self.c, "leads": self.l}

    def close(self):
        pass


class Writer:
    """Stands in for crm_write.update_company with the only_if_unsent contract."""

    def __init__(self, sent=(), fail=None):
        self.calls, self.sent, self.fail = [], set(sent), fail

    def __call__(self, company_id, only_if_unsent=False, allow_held_out=False, **columns):
        if self.fail:
            raise self.fail
        self.calls.append((company_id, only_if_unsent, columns))
        return company_id not in self.sent


class Rewrite(DbCase):
    def draft(self, cid, trk=3, third=2, banner=True, policy=True, band="B", url=None, is_test=False,
              status_="written_to_crm", batch=None, name=None):
        findings = {"company_name": name or f"Company {cid}", "tracking_cookie_count": trk, "third_party_count": third,
                    "cookie_banner": banner, "privacy_policy": policy}
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, company_name, report_url, batch)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (cid, is_test, f"c{cid}.example.com", status_, name or f"Company {cid}",
                         url if url is not None else f"{OLD}{KEY}.pdf", batch))
            cur.execute("""INSERT INTO ae_drafts (company_id, contact_id, to_email, subject, body_html, plain_text, band,
                               draft_status, template_version, findings, written_to_crm_at)
                           VALUES (%s, 'ct', 'info@x.example.com', 'OLD SUBJECT', '<p>OLD BODY</p>', 'old', %s, 'ready',
                                   'B-1.2-aaaaaaaa', %s, NOW() - interval '1 day')""", (cid, band, json.dumps(findings)))

    def row(self, cid):
        with db.get_cursor() as cur:
            cur.execute("""SELECT d.subject, d.body_html, d.template_version, d.draft_status, d.to_email, d.band, a.report_url
                           FROM ae_drafts d JOIN ae_companies a USING (company_id) WHERE d.company_id = %s""", (cid,))
            return cur.fetchone()

    def go(self, reader, writer, **kw):
        kw.setdefault("records", Path(tempfile.mkdtemp()) / "r.csv")
        with mock.patch.object(crm_write, "update_company", writer):
            return rewrite_drafts.run(reader=reader, log=lambda *_: None, **kw)

    def test_an_unsent_draft_gets_the_new_template_and_the_new_host_everywhere(self):
        self.draft(1, trk=26, third=8)
        w = Writer()
        res = self.go(Reader([1]), w)
        self.assertEqual(res["counts"], {"rewritten": 1})
        r = self.row(1)
        self.assertEqual(r["report_url"], f"{NEW}{KEY}.pdf")
        self.assertIn(f'href="{NEW}{KEY}.pdf"', r["body_html"])
        self.assertIn("View your website scan", r["body_html"])
        self.assertIn("26 tracking tools", r["body_html"])
        self.assertTrue(r["subject"].startswith("We scanned Company 1’s website—"))
        self.assertTrue(r["template_version"].startswith("V2-1.0-"))
        (cid, only_unsent, cols), = w.calls
        self.assertEqual(cid, 1)
        self.assertTrue(only_unsent)
        self.assertEqual(set(cols), {"AE_DraftSubject", "AE_DraftBody", "AE_DraftVersion", "AE_DraftGeneratedAt", "DownloadReportUrl"})
        self.assertEqual(cols["DownloadReportUrl"], f"{NEW}{KEY}.pdf")
        self.assertEqual(cols["AE_DraftVersion"], "V2-1.0")
        self.assertIs(cols["AE_DraftGeneratedAt"], crm_write.SERVER_NOW)

    def test_the_reports_table_is_kept_in_step_with_the_draft(self):
        from campaign import scan_adapter
        from tests import campaign_fixtures as fx
        self.draft(1)
        co = {"company_id": 1, "company_name": "Company 1", "domain": "c1.example.com"}
        sid = db.insert_report_scan(**scan_adapter.scan_row_from(co, fx.scan_data(domain=co["domain"]), None, 1))
        rid = db.insert_report(scan_id=sid, lead_ref=1, status="rendered", template_version="t", catalogue_version="c",
                               object_key="k", public_url=f"{OLD}{KEY}.pdf", pdf_sha256="x", size_bytes=1)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET report_ref = %s WHERE company_id = 1", (rid,))
        self.go(Reader([1]), Writer())
        with db.get_cursor() as cur:
            cur.execute("SELECT public_url FROM reports WHERE id = %s", (rid,))
            self.assertEqual(cur.fetchone()["public_url"], f"{NEW}{KEY}.pdf")

    def test_status_address_and_band_are_never_touched(self):
        self.draft(1, band="C")
        w = Writer()
        self.go(Reader([1]), w)
        r = self.row(1)
        self.assertEqual((r["draft_status"], r["to_email"], r["band"]), ("ready", "info@x.example.com", "C"))
        for forbidden in ("AE_DraftStatus", "AE_DraftEmail", "AE_DraftBand", "AE_DraftContactId", "AE_DraftSentAt"):
            self.assertNotIn(forbidden, w.calls[0][2])

    def test_a_draft_sent_in_the_meantime_is_skipped_and_nothing_changes(self):
        self.draft(1)
        before = self.row(1)
        res = self.go(Reader([1]), Writer(sent=[1]))
        self.assertEqual(res["counts"], {"skipped_sent": 1})
        self.assertEqual(self.row(1), before)

    def test_only_drafts_the_crm_lists_as_unsent_are_considered(self):
        self.draft(1)
        self.draft(2)
        w = Writer()
        self.go(Reader([1]), w)                         # 2 was sent: the CRM does not list it
        self.assertEqual([c[0] for c in w.calls], [1])
        self.assertEqual(self.row(2)["subject"], "OLD SUBJECT")

    def test_a_dry_run_renders_but_writes_nothing(self):
        self.draft(1)
        before = self.row(1)
        w = Writer()
        records = Path(tempfile.mkdtemp()) / "r.csv"
        res = self.go(Reader([1]), w, dry_run=True, records=records)
        self.assertEqual(res["counts"], {"would_rewrite": 1})
        self.assertEqual(w.calls, [])
        self.assertEqual(self.row(1), before)
        self.assertFalse(records.exists())
        self.assertEqual(len(res["samples"]), 1)

    def test_a_company_with_nothing_true_to_list_is_left_alone(self):
        self.draft(1, trk=0, third=0, banner=True, policy=True)
        before = self.row(1)
        w = Writer()
        res = self.go(Reader([1]), w)
        self.assertEqual(res["counts"], {"skipped_nothing_to_list": 1})
        self.assertEqual((w.calls, self.row(1)), ([], before))

    def test_zero_counts_fall_back_to_the_real_finding(self):
        self.draft(1, trk=0, third=0, banner=False)
        self.draft(2, trk=0, third=0, banner=True, policy=False)
        self.go(Reader([1, 2]), Writer())
        self.assertIn("No visible cookie consent banner on your website", self.row(1)["body_html"])
        self.assertIn("No privacy notice published on your website", self.row(2)["body_html"])

    def test_a_zero_third_party_count_is_not_listed(self):
        self.draft(1, trk=4, third=0)
        self.go(Reader([1]), Writer())
        body = self.row(1)["body_html"]
        self.assertIn("4 tracking tools", body)
        self.assertNotIn("third-party", body)

    def test_a_lead_is_rewritten_without_a_report_column(self):
        lead_id = settings.LEAD_ID_BASE + 5
        self.draft(lead_id)
        w = Writer()
        self.go(Reader([lead_id]), w)
        self.assertNotIn("DownloadReportUrl", w.calls[0][2])
        self.assertEqual(self.row(lead_id)["report_url"], f"{NEW}{KEY}.pdf")

    def test_a_report_url_that_is_not_ours_is_skipped(self):
        self.draft(1, url="https://example.com/whatever.pdf")
        res = self.go(Reader([1]), Writer())
        self.assertEqual(res["counts"], {"skipped_no_report_url": 1})

    def test_a_failed_crm_write_keeps_nothing_in_postgres(self):
        self.draft(1)
        before = self.row(1)
        res = self.go(Reader([1]), Writer(fail=crm_write.CrmWriteFailed("boom")))
        self.assertEqual(res["counts"], {"failed": 1})
        self.assertEqual(self.row(1), before)

    def test_one_failure_does_not_stop_the_rest(self):
        self.draft(1)
        self.draft(2)
        w = Writer()
        orig = w.__call__

        def flaky(cid, **kw):
            if cid == 1:
                raise crm_write.CrmWriteFailed("boom")
            return orig(cid, **kw)

        res = self.go(Reader([1, 2]), flaky)
        self.assertEqual(res["counts"], {"failed": 1, "rewritten": 1})

    def test_test_companies_and_unwritten_drafts_are_never_rewritten(self):
        self.draft(1, is_test=True)
        self.draft(2, status_="drafted")
        w = Writer()
        self.go(Reader([1, 2]), w)
        self.assertEqual(w.calls, [])

    def test_ids_and_limit_narrow_the_run(self):
        for i in (1, 2, 3):
            self.draft(i)
        w = Writer()
        self.go(Reader([1, 2, 3]), w, ids=[2, 3], limit=1)
        self.assertEqual([c[0] for c in w.calls], [2])

    def test_the_records_file_says_what_happened_to_each(self):
        import csv
        self.draft(1)
        self.draft(2)
        records = Path(tempfile.mkdtemp()) / "r.csv"
        self.go(Reader([1, 2]), Writer(sent=[2]), records=records)
        rows = {r["company_id"]: r for r in csv.DictReader(records.open())}
        self.assertEqual(rows["1"]["outcome"], "rewritten")
        self.assertEqual(rows["2"]["outcome"], "skipped_sent")
        self.assertEqual(rows["1"]["new_report_url"], f"{NEW}{KEY}.pdf")
        self.assertEqual(rows["1"]["old_report_url"], f"{OLD}{KEY}.pdf")

    def test_the_host_comes_from_the_setting(self):
        self.assertEqual(rewrite_drafts.new_report_url(f"{OLD}{KEY}.pdf"), f"{config.REPORT_PUBLIC_BASE_URL}/r/{KEY}.pdf")
        self.assertEqual(rewrite_drafts.new_report_url(f"{NEW}{KEY}.pdf"), f"{config.REPORT_PUBLIC_BASE_URL}/r/{KEY}.pdf")


class LinkReader:
    """What the CRM reader answers for --links-only."""

    def __init__(self, ids, emailed=()):
        self.ids, self.emailed = list(ids), set(emailed)

    def old_host_no_draft_ids(self, base):
        return list(self.ids)

    def emailed_company_ids(self, ids):
        return self.emailed & set(ids)

    def close(self):
        pass


class LinkWriter(Writer):
    def __call__(self, company_id, only_if_unsent=False, legacy_link_only=False, allow_held_out=False, **columns):
        if self.fail:
            raise self.fail
        self.calls.append((company_id, only_if_unsent, legacy_link_only, columns))
        return company_id not in self.sent


class LinksOnly(DbCase):
    def company(self, cid, status_="reported", is_test=False, url=None, with_report=False):
        url = url or f"{OLD}{KEY}.pdf"
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, report_url) VALUES (%s, %s, %s, %s, %s)""",
                        (cid, is_test, f"c{cid}.example.com", status_, url))
        if with_report:
            from campaign import scan_adapter
            from tests import campaign_fixtures as fx
            co = {"company_id": cid, "company_name": f"Company {cid}", "domain": f"c{cid}.example.com"}
            sid = db.insert_report_scan(**scan_adapter.scan_row_from(co, fx.scan_data(domain=co["domain"]), None, 1))
            rid = db.insert_report(scan_id=sid, lead_ref=cid, status="rendered", template_version="t", catalogue_version="c",
                                   object_key="k", public_url=url, pdf_sha256="x", size_bytes=1)
            with db.get_cursor() as cur:
                cur.execute("UPDATE ae_companies SET report_ref = %s WHERE company_id = %s", (rid, cid))

    def url_of(self, cid):
        with db.get_cursor() as cur:
            cur.execute("SELECT report_url FROM ae_companies WHERE company_id = %s", (cid,))
            return cur.fetchone()["report_url"]

    def go(self, reader, writer, **kw):
        kw.setdefault("records", Path(tempfile.mkdtemp()) / "r.csv")
        with mock.patch.object(crm_write, "update_company", writer):
            return rewrite_drafts.run_links(reader=reader, log=lambda *_: None, **kw)

    def test_normal_and_legacy_companies_get_only_the_link_moved(self):
        self.company(1)
        self.company(2, "legacy")
        w = LinkWriter()
        res = self.go(LinkReader([1, 2]), w)
        self.assertEqual(res["counts"], {"rewritten": 2})
        for cid, only_unsent, legacy_ok, cols in w.calls:
            self.assertTrue(only_unsent and legacy_ok)
            self.assertEqual(cols, {"DownloadReportUrl": f"{NEW}{KEY}.pdf"})
        self.assertEqual((self.url_of(1), self.url_of(2)), (f"{NEW}{KEY}.pdf",) * 2)

    def test_a_test_company_is_never_touched(self):
        self.company(1, is_test=True)
        w = LinkWriter()
        self.go(LinkReader([1]), w)
        self.assertEqual(w.calls, [])
        self.assertEqual(self.url_of(1), f"{OLD}{KEY}.pdf")

    def test_a_company_the_crm_shows_an_email_activity_for_is_skipped(self):
        self.company(1)
        self.company(2)
        w = LinkWriter()
        res = self.go(LinkReader([1, 2], emailed=[1]), w)
        self.assertEqual([c[0] for c in w.calls], [2])
        self.assertEqual(res["emailed"], 1)
        self.assertEqual(self.url_of(1), f"{OLD}{KEY}.pdf")

    def test_the_reports_table_follows_the_link(self):
        self.company(1, with_report=True)
        self.go(LinkReader([1]), LinkWriter())
        with db.get_cursor() as cur:
            cur.execute("SELECT public_url FROM reports WHERE lead_ref = 1")
            self.assertEqual(cur.fetchone()["public_url"], f"{NEW}{KEY}.pdf")

    def test_a_draft_that_was_sent_meanwhile_changes_nothing(self):
        self.company(1, with_report=True)
        res = self.go(LinkReader([1]), LinkWriter(sent=[1]))
        self.assertEqual(res["counts"], {"skipped_sent": 1})
        self.assertEqual(self.url_of(1), f"{OLD}{KEY}.pdf")
        with db.get_cursor() as cur:
            cur.execute("SELECT public_url FROM reports WHERE lead_ref = 1")
            self.assertEqual(cur.fetchone()["public_url"], f"{OLD}{KEY}.pdf")

    def test_a_dry_run_writes_nothing(self):
        self.company(1)
        w = LinkWriter()
        res = self.go(LinkReader([1]), w, dry_run=True)
        self.assertEqual((res["counts"], w.calls, self.url_of(1)), ({"would_rewrite": 1}, [], f"{OLD}{KEY}.pdf"))

    def test_a_failed_write_keeps_nothing(self):
        self.company(1, with_report=True)
        res = self.go(LinkReader([1]), LinkWriter(fail=crm_write.CrmWriteFailed("boom")))
        self.assertEqual(res["counts"], {"failed": 1})
        self.assertEqual(self.url_of(1), f"{OLD}{KEY}.pdf")


class LegacyLinkException(DbCase):
    """crm_write(legacy_link_only=True) is the one exception to 'a legacy company gets no CRM writes', and it is narrow."""

    def setUp(self):
        super().setUp()
        self.add_company(7, "legacy")
        self.add_company(8, "pending", is_test=True)
        self.conn = FakeCrmConn(sources={7: "PartnerImport", 8: "PartnerImport"})

    def update(self, cid, **kw):
        with crm_with(self.conn):
            return crm_write.update_company(cid, **kw)

    def test_a_legacy_company_is_refused_without_the_flag(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.update(7, DownloadReportUrl=f"{NEW}{KEY}.pdf")

    def test_with_the_flag_only_the_report_link_may_be_written(self):
        self.assertTrue(self.update(7, legacy_link_only=True, only_if_unsent=True, DownloadReportUrl=f"{NEW}{KEY}.pdf"))
        sql = self.conn.sent[-1][0]
        self.assertIn("DownloadReportUrl = %s", sql)
        self.assertIn("AE_DraftSentAt IS NULL", sql)

    def test_any_other_column_is_refused_even_with_the_flag(self):
        for cols in ({"AE_DraftSubject": "x"}, {"ScrapeStatus": "done"}, {"DownloadReportUrl": f"{NEW}{KEY}.pdf", "ReportStatus": "generated"}):
            with self.assertRaises(crm_write.CrmWriteRefused):
                self.update(7, legacy_link_only=True, **cols)
        self.assertEqual([q for q in self.conn.sent if q[0].startswith("UPDATE")], [])

    def test_a_test_company_is_refused_even_with_the_flag(self):
        with self.assertRaises(crm_write.TestCompanyRefused):
            self.update(8, legacy_link_only=True, DownloadReportUrl=f"{NEW}{KEY}.pdf")

    def test_a_company_not_in_ae_companies_is_refused_even_with_the_flag(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.update(99, legacy_link_only=True, DownloadReportUrl=f"{NEW}{KEY}.pdf")

    def test_the_link_must_still_be_on_the_current_host(self):
        with self.assertRaises(Exception):
            self.update(7, legacy_link_only=True, DownloadReportUrl=f"{OLD}{KEY}.pdf")


class HeldOutGuard(DbCase):
    """A GoodFirms or Clutch row can be written only from inside its own batch run (or by a tool that says so)."""

    def setUp(self):
        super().setUp()
        for i in (1, 2, 3):
            self.add_company(i, "written_to_crm")
        self.conn = FakeCrmConn(sources={1: "goodfirms", 2: "Clutch", 3: "PartnerImport", 4: None})

    def update(self, cid, **kw):
        with crm_with(self.conn):
            return crm_write.update_company(cid, AE_DraftSubject="S", **kw)

    def updates(self):
        return [q for q in self.conn.sent if q[0].startswith("UPDATE")]

    def test_outside_a_batch_a_held_out_row_is_refused_and_nothing_is_sent(self):
        for cid in (1, 2):
            with self.assertRaises(crm_write.CrmWriteRefused):
                self.update(cid)
        self.assertEqual(self.updates(), [])

    def test_inside_its_own_batch_it_is_written(self):
        from campaign import status
        with status.in_batch("goodfirms"):
            self.update(1)
        with status.in_batch("clutch"):
            self.update(2)
        self.assertEqual(len(self.updates()), 2)

    def test_inside_the_other_batch_it_is_still_refused(self):
        from campaign import status
        with status.in_batch("clutch"), self.assertRaises(crm_write.CrmWriteRefused):
            self.update(1)
        self.assertEqual(self.updates(), [])

    def test_a_tool_that_says_so_may_write_it(self):
        self.update(1, allow_held_out=True)
        self.assertEqual(len(self.updates()), 1)

    def test_every_other_source_is_unaffected(self):
        self.update(3)
        self.assertEqual(len(self.updates()), 1)

    def test_a_null_source_is_unaffected(self):
        self.add_company(4, "written_to_crm")
        self.update(4)
        self.assertEqual(len(self.updates()), 1)


class SentGuard(DbCase):
    """crm_write.update_company(only_if_unsent=True) can never overwrite a draft that has been sent."""

    def setUp(self):
        super().setUp()
        self.add_company(7, "written_to_crm")

    def update(self, conn, **kw):
        with crm_with(conn):
            return crm_write.update_company(7, **kw)

    def test_the_statement_requires_an_unsent_draft(self):
        conn = FakeCrmConn(sources={7: "PartnerImport"})
        self.assertTrue(self.update(conn, only_if_unsent=True, AE_DraftSubject="S"))
        sql, params = conn.sent[-1]
        self.assertIn("AND AE_DraftSentAt IS NULL", sql)
        self.assertEqual(conn.commits, 1)

    def test_a_sent_draft_matches_nothing_and_returns_false_without_raising(self):
        conn = FakeCrmConn(sources={7: "PartnerImport"}, update_rowcount=0)
        self.assertFalse(self.update(conn, only_if_unsent=True, AE_DraftSubject="S"))
        self.assertEqual((conn.commits, conn.rollbacks), (0, 1))

    def test_without_the_flag_nothing_changes(self):
        conn = FakeCrmConn(sources={7: "PartnerImport"})
        self.update(conn, AE_DraftSubject="S")
        self.assertNotIn("AE_DraftSentAt", conn.sent[-1][0])
        conn0 = FakeCrmConn(sources={7: "PartnerImport"}, update_rowcount=0)
        with self.assertRaises(crm_write.CrmWriteFailed):
            self.update(conn0, AE_DraftSubject="S")


if __name__ == "__main__":
    unittest.main()
