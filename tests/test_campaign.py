"""
Auto Email Campaign, milestone 1: ae_ tables, status model, claiming, intake,
test cohort, kill switch, advisory lock, and the "never write a test company to
the CRM" guard.

    docker compose run --rm --no-deps -T -v "$PWD":/app api python -m unittest tests.test_campaign -v

The pure tests need nothing. The database tests create a throwaway database on
the Postgres server DATABASE_URL points at, apply migrations/008 to it, and drop
it afterwards, so they never touch real data. If that server is unreachable they
ERROR (they are not skipped): a silent skip would look like a pass. The CRM is
replaced by an in-memory fake; nothing here connects to SQL Server.
"""
import ast
import re
import subprocess
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
import db

from campaign import (bands, crm_read, crm_write, flags, intake, jobs, locks, mirror,
                      settings, stages, status)

MIGRATION = ROOT / "migrations" / "008_auto_email_campaign.sql"
# The report tables (006, 007) plus both campaign migrations, in order.
MIGRATIONS = [ROOT / "migrations" / n for n in ("006_reports.sql", "007_report_scan_crm_values.sql",
                                                 "008_auto_email_campaign.sql", "009_campaign_stages.sql",
                                                 "010_drafting.sql")]


# --- test database ---------------------------------------------------------------
class DbCase(unittest.TestCase):
    """One throwaway database per test class."""
    _admin_url = None
    _name = None
    _orig_url = None

    @classmethod
    def setUpClass(cls):
        cls._orig_url = config.DATABASE_URL
        u = urlparse(cls._orig_url)
        cls._name = "ae_test_" + uuid.uuid4().hex[:10]
        cls._admin_url = u._replace(path="/postgres").geturl()
        admin = psycopg2.connect(cls._admin_url)
        admin.autocommit = True
        admin.cursor().execute(f'CREATE DATABASE "{cls._name}"')
        admin.close()
        db.close_pool()
        config.DATABASE_URL = u._replace(path="/" + cls._name).geturl()
        conn = psycopg2.connect(config.DATABASE_URL)
        for path in MIGRATIONS:
            conn.cursor().execute(path.read_text())
        conn.commit()
        conn.close()
        # crm_write refuses every write unless the connected database is the configured primary. A throwaway
        # test database is made "primary" here so the write-path tests can run; tests/test_campaign_safety.py
        # shows it is refused otherwise.
        cls._primary_patch = mock.patch.object(settings, "PRIMARY_DATABASE", cls._name)
        cls._primary_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._primary_patch.stop()
        db.close_pool()
        config.DATABASE_URL = cls._orig_url
        admin = psycopg2.connect(cls._admin_url)
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{cls._name}" WITH (FORCE)')
        admin.close()

    def setUp(self):
        with db.get_cursor() as cur:
            cur.execute("""TRUNCATE ae_companies, ae_contacts, ae_drafts, ae_suppression,
                           ae_verification_cache, ae_title_cache, ae_watermark, ae_mx_cache,
                           ae_neverbounce_usage, report_scans, report_findings, reports
                           RESTART IDENTITY CASCADE""")
            cur.execute("""INSERT INTO ae_flags (flag, value) VALUES ('kill_switch', 'off')
                           ON CONFLICT (flag) DO UPDATE SET value = 'off'""")

    # helpers
    def add_company(self, company_id, status_="pending", is_test=False, **kw):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status)
                           VALUES (%s, %s, %s, %s)""",
                        (company_id, is_test, kw.get("domain", f"c{company_id}.example.com"), status_))

    def status_of(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("SELECT status, status_reason FROM ae_companies WHERE company_id = %s",
                        (company_id,))
            row = cur.fetchone()
        return (row["status"], row["status_reason"]) if row else (None, None)

    def count(self, table, where="TRUE"):
        with db.get_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) n FROM {table} WHERE {where}")
            return cur.fetchone()["n"]


# --- a fake CRM --------------------------------------------------------------------
class FakeCrm:
    """What crm_read.CrmReader exposes, in memory. It has no write methods at all."""

    def __init__(self):
        self.companies = {}      # id -> {"website", "scrape_status", "report_status"}
        self.contacts_ = []      # {"contact_id","company_id","email","job_title"}
        self.emailed = set()
        self.bounced = set()

    def company(self, id_, website="acme.example.com", scrape=None, report=None, scale=None, industry=None):
        self.companies[id_] = {"website": website, "scrape_status": scrape, "report_status": report,
                               "company_scale": scale, "company_name": f"Company {id_}", "industry": industry}

    def contact(self, company_id, email, title="CEO", first_name="Asha"):
        self.contacts_.append({"contact_id": str(uuid.uuid4()), "company_id": company_id,
                               "email": email, "job_title": title, "first_name": first_name})

    # reader interface
    def companies_after(self, after_id=0):
        return [{"id": i, **c} for i, c in sorted(self.companies.items()) if i > after_id]

    def contacts(self, company_ids):
        ids = set(company_ids)
        return [c for c in self.contacts_ if c["company_id"] in ids]

    def emailed_company_ids(self, company_ids):
        return self.emailed & set(company_ids)

    def bounced_emails(self):
        return set(self.bounced)

    def status_columns(self, company_ids):
        return {i: (self.companies[i]["scrape_status"], self.companies[i]["report_status"])
                for i in company_ids if i in self.companies}


class FakeCrmConn:
    """A SQL Server connection that records what is sent instead of sending it."""

    def __init__(self, sources=None, update_rowcount=1):
        self.sources = sources if sources is not None else {}     # Id -> Source
        self.update_rowcount = update_rowcount
        self.sent, self.commits, self.rollbacks, self.closed = [], 0, 0, False

    def cursor(self):
        return FakeCrmCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeCrmCursor:
    def __init__(self, conn):
        self.conn, self._row, self.rowcount = conn, None, -1

    def execute(self, sql, params=()):
        self.conn.sent.append((sql, tuple(params)))
        if sql.lstrip().upper().startswith("SELECT"):
            cid = params[0]
            self._row = (self.conn.sources[cid],) if cid in self.conn.sources else None
        else:
            self.rowcount = self.conn.update_rowcount

    def fetchone(self):
        return self._row


def crm_with(conn):
    """Make crm_write use `conn` for the duration of a test."""
    return mock.patch.object(crm_write, "_connect", lambda: conn)


class RecordingWriter:
    """Stands in for crm_write.update_company: records calls; can be told to fail."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, company_id, **columns):
        if self.fail:
            raise self.fail
        self.calls.append((company_id, columns))


def writer(fail=None):
    w = RecordingWriter(fail)
    return w, mock.patch.object(crm_write, "update_company", w)


# --- pure tests ---------------------------------------------------------------------
class StatusModel(unittest.TestCase):
    SPEC_STATUSES = {
        "pending", "checked", "scanned", "gate_passed", "reported", "contact_ranked",
        "verified", "drafted", "written_to_crm", "sent", "expired", "skipped_invalid",
        "no_website", "scan_failed", "score_too_high", "no_gaps", "held_no_contact",
        "held_already_contacted", "suppressed",
        # added when the pipeline took over ScrapeStatus / ReportStatus:
        "unscannable",      # the site answered but could not be read (CRM: ScrapeStatus done + ReportStatus unscannable)
        "legacy",           # processed by the retired report script; imported once, no stages
        "held_duplicate_domain"}   # milestone 2: another company already holds this domain

    def test_status_list_is_exactly_the_spec(self):
        self.assertEqual(set(status.STATUSES), self.SPEC_STATUSES)

    def test_the_happy_path_is_allowed_step_by_step(self):
        path = ["pending", "checked", "scanned", "gate_passed", "reported",
                "contact_ranked", "verified", "drafted", "written_to_crm", "sent"]
        for a, b in zip(path, path[1:]):
            self.assertTrue(status.is_allowed(a, b), f"{a} -> {b}")

    def test_every_exit_status_is_reachable_from_its_stage(self):
        for a, b in [("pending", "skipped_invalid"), ("pending", "no_website"),
                     ("pending", "held_already_contacted"), ("checked", "scan_failed"),
                     ("checked", "unscannable"), ("legacy", "pending"),
                     ("scanned", "score_too_high"), ("scanned", "no_gaps"),
                     ("reported", "held_no_contact"), ("contact_ranked", "suppressed"),
                     ("drafted", "expired"), ("written_to_crm", "expired")]:
            self.assertTrue(status.is_allowed(a, b), f"{a} -> {b}")

    def test_skipping_a_stage_is_rejected(self):
        for a, b in [("pending", "scanned"), ("pending", "drafted"), ("checked", "reported"),
                     ("scanned", "reported"), ("verified", "sent"), ("drafted", "sent")]:
            self.assertFalse(status.is_allowed(a, b), f"{a} -> {b}")

    def test_going_backwards_is_rejected(self):
        for a, b in [("scanned", "checked"), ("sent", "drafted"), ("drafted", "pending")]:
            self.assertFalse(status.is_allowed(a, b), f"{a} -> {b}")

    def test_terminal_statuses_go_nowhere(self):
        for s in status.TERMINAL:
            self.assertEqual(status.TRANSITIONS[s], frozenset(), s)
        self.assertTrue({"sent", "expired", "skipped_invalid", "no_website", "scan_failed",
                         "score_too_high", "no_gaps", "held_no_contact",
                         "held_already_contacted", "suppressed"} <= set(status.TERMINAL))

    def test_unknown_status_is_never_allowed(self):
        self.assertFalse(status.is_allowed("pending", "teleported"))
        self.assertFalse(status.is_allowed("teleported", "pending"))

    def test_every_declared_target_is_a_real_status(self):
        for a, targets in status.TRANSITIONS.items():
            self.assertIn(a, status.STATUSES)
            self.assertTrue(set(targets) <= set(status.STATUSES), a)

    def test_stub_stages_only_declare_allowed_transitions(self):
        self.assertEqual([s.number for s in stages.STAGES], list(range(1, 11)))
        for stage in stages.STAGES:
            for src, dst in stage.transitions:
                self.assertTrue(status.is_allowed(src, dst), f"stage {stage.number}: {src} -> {dst}")
        for stage in stages.STAGES:
            self.assertTrue(stage.implemented, stage.name)      # milestones 1-3 built every stage
        self.assertEqual([s.name for s in stages.STAGES][-2:], ["draft", "write_drafts"])


class Bands(unittest.TestCase):
    def test_edges(self):
        cases = {0: "A", 24: "A", 25: "B", 50: "B", 51: "C", 74: "C", 75: "D", 90: "D",
                 91: None, 100: None}
        for score, band in cases.items():
            self.assertEqual(bands.band_for(score), band, score)

    def test_missing_or_out_of_range_has_no_band(self):
        self.assertIsNone(bands.band_for(None))
        self.assertIsNone(bands.band_for(-1))
        self.assertIsNone(bands.band_for(101))

    def test_band_b_matches_the_report_priority_label(self):
        import fingerprints
        self.assertEqual(settings.BAND_CUTOFFS["B"][1], fingerprints.SEVERE_AT)


class CrmIsReadOnly(unittest.TestCase):
    def test_select_is_allowed(self):
        crm_read.assert_read_only("SELECT Id FROM dbo.CRM_Scrapper WHERE Source = %s")
        crm_read.assert_read_only("  select 1")

    def test_anything_else_is_refused(self):
        for sql in ("UPDATE dbo.CRM_Scrapper SET Website = 'x'",
                    "DELETE FROM dbo.CRM_Scrapper",
                    "INSERT INTO dbo.CRM_Scrapper (CompanyName) VALUES ('x')",
                    "ALTER TABLE dbo.CRM_Scrapper ADD AE_Subject nvarchar(max)",
                    "EXEC sp_who",
                    "SELECT 1; DROP TABLE dbo.CRM_Scrapper",
                    "SELECT * INTO dbo.copy FROM dbo.CRM_Scrapper",
                    "WITH x AS (SELECT 1) UPDATE dbo.CRM_Scrapper SET Website = NULL"):
            with self.assertRaises(crm_read.NotReadOnly, msg=sql):
                crm_read.assert_read_only(sql)

    def test_the_reader_has_no_write_methods(self):
        names = [n for n in dir(crm_read.CrmReader) if not n.startswith("_")]
        for n in names:
            self.assertFalse(re.match(r"(write|update|insert|delete|set|mark)", n), n)

    def test_the_module_source_sends_no_write_sql(self):
        src = (ROOT / "campaign" / "crm_read.py").read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith(("#", '"""')))
        for kw in ("INSERT INTO", "UPDATE dbo", "UPDATE crm", "DELETE FROM", "ALTER TABLE"):
            self.assertNotIn(kw, code.replace("assert_read_only", ""))


class Domain(unittest.TestCase):
    def test_domain_of(self):
        self.assertEqual(intake.domain_of("https://www.Acme.in/about?x=1"), "acme.in")
        self.assertEqual(intake.domain_of("acme.com."), "acme.com")
        self.assertIsNone(intake.domain_of(""))
        self.assertIsNone(intake.domain_of(None))
        self.assertIsNone(intake.domain_of("not a domain"))


class EmailClassification(unittest.TestCase):
    def test_business_vs_free_mail(self):
        self.assertTrue(intake.is_business_email("a@acme.in"))
        self.assertFalse(intake.is_business_email("a@gmail.com"))
        self.assertFalse(intake.is_business_email("a@Yahoo.co.in"))
        self.assertFalse(intake.is_business_email("a@rediffmail.com"))
        self.assertFalse(intake.is_business_email(""))
        self.assertFalse(intake.is_business_email(None))
        self.assertFalse(intake.is_business_email("no-at-sign"))


class CrmWriteColumns(DbCase):
    """crm_write.update_company is the only CRM write: two columns plus AE_, PartnerImport only."""

    def setUp(self):
        super().setUp()
        self.add_company(7)                  # production, pending
        self.conn = FakeCrmConn(sources={7: "PartnerImport", 8: "CsvImport", 9: None})

    def update(self, company_id=7, **cols):
        with crm_with(self.conn):
            return crm_write.update_company(company_id, **cols)

    def test_the_two_status_columns_are_allowed(self):
        self.update(ScrapeStatus="done", ReportStatus="generated")
        sql, params = self.conn.sent[-1]
        self.assertTrue(sql.startswith("UPDATE dbo.CRM_Scrapper SET "))
        self.assertIn("ScrapeStatus = %s", sql)
        self.assertIn("ReportStatus = %s", sql)
        self.assertEqual(self.conn.commits, 1)

    def test_one_column_alone_is_allowed(self):
        self.update(ScrapeStatus="failed")
        self.assertEqual(self.conn.sent[-1][1][0], "failed")

    def test_the_update_is_pinned_to_the_row_and_to_partner_import(self):
        self.update(ScrapeStatus="done")
        sql, params = self.conn.sent[-1]
        self.assertTrue(sql.endswith("WHERE Id = %s AND Source = %s"), sql)
        self.assertEqual(params, ("done", 7, "PartnerImport"))

    def test_values_travel_as_parameters_never_in_the_sql_text(self):
        self.update(ScrapeStatus="done", ReportStatus="no_gap")
        sql = self.conn.sent[-1][0]
        for value in ("done", "no_gap"):
            self.assertNotIn(value, sql)

    def test_the_future_ae_columns_are_allowed(self):
        valid = {"AE_DraftBand": "B", "AE_DraftBody": "<p>x</p>", "AE_DraftEmail": "a@b.co",
                 "AE_DraftStatus": "expired", "AE_DraftSubject": "Subject", "AE_DraftVersion": "B-0.1-placeholder",
                 "AE_DraftGeneratedAt": crm_write.SERVER_NOW, "AE_DraftContactId": 7, "AE_DraftSentAt": "x"}
        for col in settings.AE_COLUMNS:
            self.conn.sent.clear()
            self.update(**{col: valid[col]})
            self.assertTrue(f"{col} = %s" in self.conn.sent[-1][0] or f"{col} = SYSUTCDATETIME()" in self.conn.sent[-1][0])

    def test_the_ae_columns_are_the_ones_the_crm_team_created(self):
        self.assertEqual(set(settings.AE_COLUMNS), {
            "AE_DraftBand", "AE_DraftBody", "AE_DraftContactId", "AE_DraftEmail",
            "AE_DraftGeneratedAt", "AE_DraftSentAt", "AE_DraftStatus", "AE_DraftSubject",
            "AE_DraftVersion"})

    def test_every_other_column_is_refused_before_a_connection_is_opened(self):
        for col in ("ScrapedAt", "ScrapeAttempts", "Website", "Source", "Id", "CompanyName",
                    "CompanyScale", "Industry", "CreatedAt",
                    "ContactName", "LushaWorkEmail", "TargetTitle1", "AE_Other", "ae_draftbody",
                    "ScrapeStatus = 'x', Website", "ScrapeStatus; DROP TABLE dbo.CRM_Scrapper--"):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=col):
                self.update(**{col: "x"})
        self.assertEqual(self.conn.sent, [])

    def test_a_mix_of_allowed_and_refused_writes_nothing(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.update(ScrapeStatus="done", Website="evil.example.com")
        self.assertEqual(self.conn.sent, [])

    def test_no_columns_is_refused(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.update()

    def test_only_the_existing_vocabulary_may_be_written(self):
        for col, bad in (("ScrapeStatus", "running"), ("ScrapeStatus", "no_website"),
                         ("ScrapeStatus", "Done"), ("ScrapeStatus", ""), ("ScrapeStatus", None),
                         ("ReportStatus", "failed"), ("ReportStatus", "ready"),
                         ("ReportStatus", "GENERATED"), ("ReportStatus", None)):
            with self.assertRaises(crm_write.CrmWriteRefused, msg=f"{col}={bad!r}"):
                self.update(**{col: bad})
        self.assertEqual(self.conn.sent, [])

    def test_the_vocabulary_is_the_one_written_by_the_old_job(self):
        self.assertEqual(settings.VOCAB["ScrapeStatus"], {"done", "failed"})
        self.assertEqual(settings.VOCAB["ReportStatus"], {"generated", "no_gap", "unscannable"})

    def test_a_non_partner_import_row_is_refused_and_no_update_is_sent(self):
        for cid in (8, 9, 404):       # CsvImport, NULL source, a row that does not exist
            self.add_company(cid)
            with self.assertRaises(crm_write.CrmWriteRefused, msg=cid):
                self.update(cid, ScrapeStatus="done")
        self.assertEqual([q for q in self.conn.sent if q[0].lstrip().upper().startswith("UPDATE")], [])

    def test_an_update_that_matches_no_row_is_rolled_back_and_raised(self):
        self.conn.update_rowcount = 0
        with self.assertRaises(crm_write.CrmWriteFailed):
            self.update(ScrapeStatus="done")
        self.assertEqual((self.conn.commits, self.conn.rollbacks), (0, 1))

    def test_the_connection_is_always_closed(self):
        self.update(ScrapeStatus="done")
        self.assertTrue(self.conn.closed)

    def test_a_test_company_is_refused_for_every_column_including_status_and_drafts(self):
        self.add_company(20, "pending", is_test=True)
        for cols in ({"ScrapeStatus": "done"}, {"ReportStatus": "generated"},
                     {"AE_DraftBody": "<p>x</p>"}, {"ScrapeStatus": "done", "ReportStatus": "no_gap"}):
            with self.assertRaises(crm_write.TestCompanyRefused, msg=str(cols)):
                self.update(20, **cols)
        self.assertEqual(self.conn.sent, [])

    def test_no_crm_connection_is_opened_for_a_test_company(self):
        self.add_company(20, "pending", is_test=True)
        opened = []
        with mock.patch.object(crm_write, "_connect", lambda: opened.append(1)):
            with self.assertRaises(crm_write.TestCompanyRefused):
                crm_write.update_company(20, ScrapeStatus="done")
        self.assertEqual(opened, [])

    def test_a_company_we_do_not_hold_is_refused(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.update(999, ScrapeStatus="done")

    def test_a_legacy_company_is_refused(self):
        self.add_company(30, "legacy")
        with self.assertRaises(crm_write.CrmWriteRefused):
            self.update(30, ScrapeStatus="done")
        self.assertEqual(self.conn.sent, [])


class WriteDraftGuard(DbCase):
    """The draft write is still a stub; its guard is real."""

    def test_a_test_company_is_refused(self):
        self.add_company(7, "drafted", is_test=True)
        with self.assertRaises(crm_write.TestCompanyRefused):
            crm_write.write_draft(7)

    def test_a_test_company_is_refused_even_if_the_caller_says_it_is_not(self):
        self.add_company(7, "drafted", is_test=True)
        with self.assertRaises(crm_write.TestCompanyRefused):
            crm_write.write_draft(7, is_test=False)

    def test_a_company_we_do_not_know_is_refused(self):
        with self.assertRaises(crm_write.CrmWriteRefused):
            crm_write.write_draft(999)

    def test_a_production_company_gets_past_the_guard_and_hits_the_milestone_stub(self):
        self.add_company(8, "drafted", is_test=False)
        with self.assertRaises(NotImplementedError):
            crm_write.write_draft(8)


class WhoWritesToTheCrm(unittest.TestCase):
    def test_only_crm_write_builds_a_write_against_the_crm_tables(self):
        for path in (ROOT / "campaign").glob("*.py"):
            if path.name == "crm_write.py":
                continue
            src = path.read_text()
            self.assertIsNone(re.search(r"(UPDATE|INSERT INTO|DELETE FROM)\s+\{", src), path.name)
            if path.name != "crm_read.py":
                self.assertNotIn("pymssql", src, path.name)

    def test_crm_write_goes_through_the_one_connection_helper(self):
        src = (ROOT / "campaign" / "crm_write.py").read_text()
        self.assertNotIn("pymssql", src)
        self.assertEqual(len(re.findall(r"\bUPDATE\b", src.split('"""', 2)[-1])), 1,
                         "exactly one UPDATE statement in crm_write.py")


# --- database tests -------------------------------------------------------------------
class SetStatus(DbCase):
    def test_a_valid_change_is_applied_with_its_reason(self):
        self.add_company(1)
        with db.get_cursor() as cur:
            status.set_status(cur, 1, "checked", "free checks passed")
        self.assertEqual(self.status_of(1), ("checked", "free checks passed"))

    def test_an_invalid_change_raises_and_changes_nothing(self):
        self.add_company(1)
        with self.assertRaises(status.InvalidTransition):
            with db.get_cursor() as cur:
                status.set_status(cur, 1, "drafted", "skipping ahead")
        self.assertEqual(self.status_of(1)[0], "pending")

    def test_a_reason_is_mandatory(self):
        self.add_company(1)
        for reason in ("", "   ", None):
            with self.assertRaises(ValueError):
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, "checked", reason)
        self.assertEqual(self.status_of(1)[0], "pending")

    def test_an_unknown_company_raises(self):
        with self.assertRaises(status.UnknownCompany):
            with db.get_cursor() as cur:
                status.set_status(cur, 404, "checked", "x")

    def test_a_terminal_status_cannot_be_left(self):
        self.add_company(1, "no_gaps")
        with self.assertRaises(status.InvalidTransition):
            with db.get_cursor() as cur:
                status.set_status(cur, 1, "gate_passed", "second thoughts")

    def test_updated_at_moves(self):
        self.add_company(1)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET updated_at = NOW() - INTERVAL '1 day'")
            status.set_status(cur, 1, "checked", "ok")
            cur.execute("SELECT updated_at > NOW() - INTERVAL '1 hour' AS fresh FROM ae_companies")
            self.assertTrue(cur.fetchone()["fresh"])

    def test_the_status_column_rejects_an_unknown_value_even_with_raw_sql(self):
        self.add_company(1)
        with self.assertRaises(psycopg2.errors.CheckViolation):
            with db.get_cursor() as cur:
                cur.execute("UPDATE ae_companies SET status = 'teleported'")


class Claiming(DbCase):
    def test_a_claim_takes_only_the_requested_status_and_limit(self):
        for i in range(1, 6):
            self.add_company(i)
        self.add_company(6, "checked")
        with status.claim("pending", 3) as (cur, rows):
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(r["status"] == "pending" for r in rows))

    def test_concurrent_claims_never_take_the_same_row(self):
        for i in range(1, 41):
            self.add_company(i)
        taken, errors = [], []
        start = threading.Barrier(4)
        hold = threading.Barrier(4)

        def worker():
            try:
                start.wait(10)
                with status.claim("pending", 10) as (cur, rows):
                    taken.append([r["company_id"] for r in rows])
                    hold.wait(10)          # every claim is open at once
            except Exception as exc:       # noqa: BLE001 -- surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        self.assertEqual(errors, [])
        flat = [c for batch in taken for c in batch]
        self.assertEqual(len(flat), 40)
        self.assertEqual(len(set(flat)), 40, "a row was claimed twice")

    def test_a_locked_row_is_skipped_not_waited_for(self):
        self.add_company(1)
        self.add_company(2)
        with status.claim("pending", 1) as (cur, first):
            with status.claim("pending", 5) as (cur2, second):
                self.assertEqual([r["company_id"] for r in second],
                                 [r for r in (1, 2) if r != first[0]["company_id"]])

    def test_a_status_change_inside_a_claim_commits_with_it(self):
        self.add_company(1)
        with status.claim("pending", 5) as (cur, rows):
            status.set_status(cur, rows[0]["company_id"], "checked", "ok")
        self.assertEqual(self.status_of(1)[0], "checked")

    def test_an_error_inside_a_claim_rolls_back_and_releases_the_row(self):
        self.add_company(1)
        with self.assertRaises(RuntimeError):
            with status.claim("pending", 5) as (cur, rows):
                status.set_status(cur, 1, "checked", "ok")
                raise RuntimeError("boom")
        self.assertEqual(self.status_of(1)[0], "pending")
        with status.claim("pending", 5) as (cur, rows):
            self.assertEqual(len(rows), 1)


class ScanRetries(DbCase):
    def test_two_retries_then_scan_failed_and_the_crm_hears_about_it_once(self):
        self.add_company(1, "checked")
        w, patch = writer()
        results = []
        with patch:
            for _ in range(3):
                with db.get_cursor() as cur:
                    results.append(stages.record_scan_failure(cur, 1, "timeout"))
        self.assertEqual(results, ["retry", "retry", "scan_failed"])
        self.assertEqual(self.status_of(1)[0], "scan_failed")
        self.assertEqual(w.calls, [(1, {"ScrapeStatus": "failed"})])
        with db.get_cursor() as cur:
            cur.execute("SELECT scan_attempts FROM ae_companies WHERE company_id = 1")
            self.assertEqual(cur.fetchone()["scan_attempts"], 3)

    def test_a_retry_sets_next_retry_at_keeps_the_status_and_writes_nothing_to_the_crm(self):
        self.add_company(1, "checked")
        w, patch = writer()
        with patch, db.get_cursor() as cur:
            self.assertEqual(stages.record_scan_failure(cur, 1, "timeout"), "retry")
            cur.execute("SELECT status, next_retry_at > NOW() AS later FROM ae_companies")
            row = cur.fetchone()
        self.assertEqual(row["status"], "checked")
        self.assertTrue(row["later"])
        self.assertEqual(w.calls, [])

    def test_if_the_crm_write_fails_the_final_failure_is_not_recorded_so_it_retries(self):
        self.add_company(1, "checked")
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET scan_attempts = 2 WHERE company_id = 1")
        w, patch = writer(fail=crm_write.CrmWriteFailed("down"))
        with patch:
            with self.assertRaises(crm_write.CrmWriteFailed):
                with db.get_cursor() as cur:
                    stages.record_scan_failure(cur, 1, "timeout")
        self.assertEqual(self.status_of(1)[0], "checked")
        with db.get_cursor() as cur:
            cur.execute("SELECT scan_attempts FROM ae_companies WHERE company_id = 1")
            self.assertEqual(cur.fetchone()["scan_attempts"], 2, "the attempt count rolled back too")


class MirroredStatusChanges(DbCase):
    """ScrapeStatus / ReportStatus on the CRM mirror ae_companies, in the same step."""

    CASES = [   # (from, to, what the CRM is told)
        ("checked", "scanned", {"ScrapeStatus": "done"}),
        ("checked", "scan_failed", {"ScrapeStatus": "failed"}),
        ("checked", "unscannable", {"ScrapeStatus": "done", "ReportStatus": "unscannable"}),
        ("scanned", "no_gaps", {"ReportStatus": "no_gap"}),
        ("gate_passed", "reported", {"ReportStatus": "generated"}),
    ]
    SILENT = [("pending", "checked"), ("pending", "skipped_invalid"), ("pending", "no_website"),
              ("pending", "held_already_contacted"), ("scanned", "gate_passed"),
              ("scanned", "score_too_high"), ("reported", "contact_ranked"),
              ("reported", "held_no_contact"), ("contact_ranked", "verified"),
              ("contact_ranked", "suppressed"), ("verified", "drafted"),
              ("drafted", "written_to_crm"), ("written_to_crm", "sent"), ("drafted", "expired")]

    def move(self, company_id, new, w_patch):
        with w_patch, db.get_cursor() as cur:
            status.set_status(cur, company_id, new, f"-> {new}")

    def test_each_change_writes_exactly_its_columns_once(self):
        for i, (src, dst, cols) in enumerate(self.CASES):
            self.add_company(100 + i, src)
            w, patch = writer()
            self.move(100 + i, dst, patch)
            self.assertEqual(self.status_of(100 + i)[0], dst)
            self.assertEqual(w.calls, [(100 + i, cols)], f"{src} -> {dst}")

    def test_every_other_change_leaves_the_crm_alone(self):
        for i, (src, dst) in enumerate(self.SILENT):
            self.add_company(200 + i, src)
            w, patch = writer()
            self.move(200 + i, dst, patch)
            self.assertEqual(w.calls, [], f"{src} -> {dst}")

    def test_the_mirror_table_is_exactly_the_five_rules(self):
        self.assertEqual(mirror.CRM_VALUES, {
            "scanned": {"ScrapeStatus": "done"},
            "scan_failed": {"ScrapeStatus": "failed"},
            "unscannable": {"ScrapeStatus": "done", "ReportStatus": "unscannable"},
            "no_gaps": {"ReportStatus": "no_gap"},
            "reported": {"ReportStatus": "generated"}})

    def test_every_mirrored_value_is_in_the_existing_vocabulary(self):
        for st, cols in mirror.CRM_VALUES.items():
            self.assertIn(st, status.STATUSES)
            for col, val in cols.items():
                self.assertIn(val, settings.VOCAB[col], f"{st}: {col}={val}")

    def test_a_failed_crm_write_leaves_the_status_alone(self):
        self.add_company(1, "checked")
        w, patch = writer(fail=crm_write.CrmWriteFailed("connection dropped"))
        with patch:
            with self.assertRaises(crm_write.CrmWriteFailed):
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, "scanned", "scan ok")
        self.assertEqual(self.status_of(1)[0], "checked")

    def test_a_failed_crm_write_cannot_be_papered_over_by_the_caller(self):
        """Even if the caller swallows the error and commits, ae_companies must not move."""
        self.add_company(1, "checked")
        w, patch = writer(fail=crm_write.CrmWriteFailed("connection dropped"))
        with patch, db.get_cursor() as cur:
            try:
                status.set_status(cur, 1, "scanned", "scan ok")
            except crm_write.CrmWriteFailed:
                pass
        self.assertEqual(self.status_of(1), ("checked", None))

    def test_a_refused_crm_write_also_blocks_the_change(self):
        self.add_company(1, "checked")
        w, patch = writer(fail=crm_write.CrmWriteRefused("not PartnerImport"))
        with patch:
            with self.assertRaises(crm_write.CrmWriteRefused):
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, "scanned", "scan ok")
        self.assertEqual(self.status_of(1)[0], "checked")

    def test_an_invalid_change_never_reaches_the_crm(self):
        self.add_company(1, "pending")
        w, patch = writer()
        with patch:
            with self.assertRaises(status.InvalidTransition):
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, "reported", "skipping ahead")
        self.assertEqual(w.calls, [])

    def test_a_test_company_moves_through_every_stage_without_one_crm_write(self):
        path = ["checked", "scanned", "gate_passed", "reported", "contact_ranked", "verified",
                "drafted", "written_to_crm", "sent"]
        self.add_company(1, "pending", is_test=True)
        w, patch = writer()
        with patch:
            for nxt in path:
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, nxt, f"-> {nxt}")
        self.assertEqual(self.status_of(1)[0], "sent")
        self.assertEqual(w.calls, [], "a test company must never cause a CRM write")

    def test_a_test_company_that_fails_or_is_unscannable_is_not_written_either(self):
        for i, dst in enumerate(("scan_failed", "unscannable")):
            self.add_company(10 + i, "checked", is_test=True)
            w, patch = writer()
            self.move(10 + i, dst, patch)
            self.assertEqual(w.calls, [], dst)

    def test_end_to_end_through_the_real_write_function(self):
        self.add_company(1, "checked")
        conn = FakeCrmConn(sources={1: "PartnerImport"})
        with crm_with(conn), db.get_cursor() as cur:
            status.set_status(cur, 1, "unscannable", "HTTP 403")
        self.assertEqual(self.status_of(1)[0], "unscannable")
        updates = [q for q in conn.sent if q[0].upper().startswith("UPDATE")]
        self.assertEqual(len(updates), 1)
        self.assertIn("ScrapeStatus = %s", updates[0][0])
        self.assertIn("ReportStatus = %s", updates[0][0])
        self.assertEqual(updates[0][1], ("done", "unscannable", 1, "PartnerImport"))

    def test_end_to_end_a_test_company_opens_no_connection_at_all(self):
        self.add_company(1, "checked", is_test=True)
        opened = []
        with mock.patch.object(crm_write, "_connect", lambda: opened.append(1)), db.get_cursor() as cur:
            status.set_status(cur, 1, "scanned", "ok")
        self.assertEqual(opened, [])


class LegacyRows(DbCase):
    def test_a_legacy_company_cannot_be_moved_by_the_normal_path(self):
        self.add_company(1, "legacy")
        for target in ("pending", "checked"):
            with self.assertRaises(status.InvalidTransition):
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, target, "reopen")
        self.assertEqual(self.status_of(1)[0], "legacy")

    def test_only_a_test_company_may_leave_legacy_and_only_for_pending(self):
        self.add_company(1, "legacy", is_test=True)
        with db.get_cursor() as cur:
            status.set_status(cur, 1, "pending", "promoted")
        self.assertEqual(self.status_of(1)[0], "pending")

    def test_a_legacy_company_never_triggers_a_crm_write(self):
        self.add_company(1, "legacy")
        w, patch = writer()
        with patch:
            with self.assertRaises(status.InvalidTransition):
                with db.get_cursor() as cur:
                    status.set_status(cur, 1, "scanned", "x")
        self.assertEqual(w.calls, [])

    def test_promote_to_test_marks_it_and_reopens_it(self):
        self.add_company(1, "legacy")
        with db.get_cursor() as cur:
            intake.promote_to_test(cur, 1)
        with db.get_cursor() as cur:
            cur.execute("SELECT status, is_test FROM ae_companies WHERE company_id = 1")
            self.assertEqual(dict(cur.fetchone()), {"status": "pending", "is_test": True})

    def test_promote_refuses_anything_that_is_not_legacy(self):
        self.add_company(1, "pending")
        self.add_company(2, "legacy", is_test=True)
        for cid in (1, 2, 99):
            with self.assertRaises((status.InvalidTransition, status.UnknownCompany)):
                with db.get_cursor() as cur:
                    intake.promote_to_test(cur, cid)


class MirrorAudit(DbCase):
    """ae_companies is the truth; the audit reports where the CRM columns disagree."""

    def setUp(self):
        super().setUp()
        self.crm = FakeCrm()

    def test_expected_values_follow_the_status(self):
        self.assertEqual(mirror.expected("scanned"), ("done", None))
        self.assertEqual(mirror.expected("scan_failed"), ("failed", None))
        self.assertEqual(mirror.expected("unscannable"), ("done", "unscannable"))
        self.assertEqual(mirror.expected("no_gaps"), ("done", "no_gap"))
        self.assertEqual(mirror.expected("reported"), ("done", "generated"))
        self.assertEqual(mirror.expected("sent"), ("done", "generated"))
        self.assertEqual(mirror.expected("pending"), (None, None))
        self.assertEqual(mirror.expected("score_too_high"), ("done", None))

    def test_agreement_is_silent(self):
        self.add_company(1, "reported")
        self.crm.company(1, scrape="done", report="generated")
        self.assertEqual(mirror.audit(self.crm), [])

    def test_a_disagreement_is_reported_with_both_sides(self):
        self.add_company(1, "reported")
        self.crm.company(1, scrape="done", report=None)
        found = mirror.audit(self.crm)
        self.assertEqual(found, [{"company_id": 1, "status": "reported",
                                  "expected": ("done", "generated"), "crm": ("done", None)}])

    def test_test_and_legacy_companies_are_not_audited(self):
        self.add_company(1, "reported", is_test=True)
        self.add_company(2, "legacy")
        self.crm.company(1, scrape=None, report=None)
        self.crm.company(2, scrape="done", report="generated")
        self.assertEqual(mirror.audit(self.crm), [])

    def test_statuses_with_no_expectation_are_not_compared(self):
        self.add_company(1, "pending")
        self.crm.company(1, scrape="failed", report=None)       # an old value, ours to overwrite later
        self.assertEqual(mirror.audit(self.crm), [])


class KillSwitch(DbCase):
    def test_off_by_default(self):
        self.assertFalse(flags.kill_switch_on())

    def test_on_means_on(self):
        flags.set_flag("kill_switch", "on")
        self.assertTrue(flags.kill_switch_on())

    def test_a_missing_flag_fails_closed(self):
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM ae_flags WHERE flag = 'kill_switch'")
        self.assertTrue(flags.kill_switch_on())

    def test_it_stops_every_job_before_any_work(self):
        flags.set_flag("kill_switch", "on")
        for name in jobs.JOBS:
            ran = []
            outcome = jobs.run_job(name, lambda **kw: ran.append(1))
            self.assertEqual(outcome, "killed", name)
            self.assertEqual(ran, [], name)

    def test_it_is_checked_before_the_lock_is_taken(self):
        flags.set_flag("kill_switch", "on")
        with locks.job_lock("prepare") as got:
            self.assertTrue(got)
            self.assertEqual(jobs.run_job("prepare", lambda **kw: None), "killed")

    def test_every_registered_job_goes_through_run_job(self):
        self.assertEqual(set(jobs.JOBS), {"prepare", "housekeeping"})


class AdvisoryLock(DbCase):
    def test_a_second_holder_is_refused_until_the_first_lets_go(self):
        with locks.job_lock("prepare") as first:
            self.assertTrue(first)
            with locks.job_lock("prepare") as second:
                self.assertFalse(second)
        with locks.job_lock("prepare") as again:
            self.assertTrue(again)

    def test_different_jobs_do_not_block_each_other(self):
        with locks.job_lock("prepare") as a:
            with locks.job_lock("housekeeping") as b:
                self.assertTrue(a and b)

    def test_a_second_run_exits_without_working(self):
        ran = []
        with locks.job_lock("prepare"):
            outcome = jobs.run_job("prepare", lambda **kw: ran.append(1))
        self.assertEqual(outcome, "locked")
        self.assertEqual(ran, [])

    def test_the_lock_is_released_after_an_exception(self):
        with self.assertRaises(RuntimeError):
            with locks.job_lock("prepare"):
                raise RuntimeError("boom")
        with locks.job_lock("prepare") as got:
            self.assertTrue(got)

    def test_run_job_passes_its_options_to_the_job(self):
        seen = {}
        outcome = jobs.run_job("prepare", lambda **kw: seen.update(kw), dry_run=True, limit=5, sample=2)
        self.assertEqual(outcome, "ok")
        self.assertEqual(seen, {"dry_run": True, "limit": 5, "sample": 2})


class Intake(DbCase):
    def setUp(self):
        super().setUp()
        self.crm = FakeCrm()

    def run_intake(self, **kw):
        return intake.run_intake(self.crm, **kw)

    def test_inserts_companies_and_contacts_as_pending(self):
        self.crm.company(10)
        self.crm.contact(10, "ceo@acme.example.com", "CEO")
        self.crm.contact(10, "ops@gmail.com", None)
        counts = self.run_intake()
        self.assertEqual(self.status_of(10)[0], "pending")
        self.assertEqual(self.count("ae_contacts", "company_id = 10 AND status = 'pending'"), 2)
        self.assertEqual(counts["companies_new"], 1)
        self.assertEqual(counts["contacts_new"], 2)
        self.assertEqual(counts["contacts_business_email"], 1)

    def test_the_company_row_carries_the_domain_and_the_contact_its_title(self):
        self.crm.company(10, website="https://www.Acme.in/home")
        self.crm.contact(10, "ceo@acme.in", "Chief Executive Officer")
        self.run_intake()
        with db.get_cursor() as cur:
            cur.execute("SELECT domain, is_test FROM ae_companies WHERE company_id = 10")
            self.assertEqual(dict(cur.fetchone()), {"domain": "acme.in", "is_test": False})
            cur.execute("SELECT job_title, email, nb_decision FROM ae_contacts WHERE company_id = 10")
            self.assertEqual(dict(cur.fetchone()), {"job_title": "Chief Executive Officer",
                                                    "email": "ceo@acme.in", "nb_decision": "NOT_CHECKED"})

    def test_production_takes_every_company_whatever_its_scrape_status(self):
        for i, scrape in enumerate((None, "done", "failed", "no_website"), start=1):
            self.crm.company(i, scrape=scrape)
        counts = self.run_intake()
        self.assertEqual(counts["by_status"], {"pending": 4})
        self.assertEqual(self.count("ae_companies", "NOT is_test"), 4)

    def test_intake_is_idempotent(self):
        for i in (1, 2, 3):
            self.crm.company(i)
            self.crm.contact(i, f"a{i}@c{i}.example.com")
        self.crm.company(4, report="generated")
        self.crm.bounced = {"x@y.example.com"}
        self.crm.emailed = {3}
        self.run_intake()
        before = {t: self.count(t) for t in ("ae_companies", "ae_contacts", "ae_suppression")}
        again = self.run_intake()
        after = {t: self.count(t) for t in before}
        self.assertEqual(before, after)
        for key in ("companies_new", "legacy_new", "contacts_new", "suppression_new"):
            self.assertEqual(again[key], 0, key)

    def test_a_second_run_does_not_reopen_a_company_it_already_moved(self):
        self.crm.company(1)
        self.run_intake()
        with db.get_cursor() as cur:
            status.set_status(cur, 1, "checked", "free checks passed")
        self.run_intake()
        self.assertEqual(self.status_of(1)[0], "checked")

    def test_a_crm_bounce_lands_in_suppression_with_its_source(self):
        self.crm.bounced = {"Bad@Acme.example.com", "gone@x.example.com"}
        counts = self.run_intake()
        with db.get_cursor() as cur:
            cur.execute("SELECT email, reason, source FROM ae_suppression ORDER BY email")
            rows = [dict(r) for r in cur.fetchall()]
        self.assertEqual(rows, [
            {"email": "bad@acme.example.com", "reason": "bounced", "source": "crm_history"},
            {"email": "gone@x.example.com", "reason": "bounced", "source": "crm_history"}])
        self.assertEqual(counts["suppression_new"], 2)

    def test_existing_suppression_rows_are_not_overwritten(self):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_suppression (email, reason, source)
                           VALUES ('bad@acme.example.com', 'unsubscribed', 'manual')""")
        self.crm.bounced = {"bad@acme.example.com"}
        self.run_intake()
        with db.get_cursor() as cur:
            cur.execute("SELECT reason, source FROM ae_suppression")
            self.assertEqual(dict(cur.fetchone()), {"reason": "unsubscribed", "source": "manual"})

    def test_an_already_emailed_company_is_held(self):
        self.crm.company(1)
        self.crm.company(2)
        self.crm.emailed = {1}
        counts = self.run_intake()
        self.assertEqual(self.status_of(1),
                         ("held_already_contacted", "CRM already has an Email activity for this company"))
        self.assertEqual(self.status_of(2)[0], "pending")
        self.assertEqual(counts["held_already_contacted"], 1)

    def test_the_hold_can_be_switched_off(self):
        self.crm.company(1)
        self.crm.emailed = {1}
        self.run_intake(exclude_previously_emailed=False)
        self.assertEqual(self.status_of(1)[0], "pending")

    def test_a_company_with_no_website_stays_pending_for_stage_3_to_decide(self):
        self.crm.company(1, website="")
        self.crm.company(2, website=None)
        self.run_intake()
        self.assertEqual(self.status_of(1)[0], "pending")
        self.assertEqual(self.status_of(2)[0], "pending")
        with db.get_cursor() as cur:
            cur.execute("SELECT domain, domain_source FROM ae_companies WHERE company_id = 1")
            self.assertEqual(dict(cur.fetchone()), {"domain": None, "domain_source": None})

    def test_only_ids_takes_in_just_those_companies(self):
        for i in (1, 2, 3):
            self.crm.company(i)
        counts = self.run_intake(only_ids={2, 3})
        self.assertEqual(counts["companies_new"], 2)
        self.assertEqual(self.status_of(1), (None, None))
        self.assertEqual(self.status_of(2)[0], "pending")

    def test_only_ids_does_not_advance_the_watermark_past_companies_it_skipped(self):
        for i in (1, 2, 3):
            self.crm.company(i)
        self.run_intake(only_ids={3})
        self.run_intake()
        self.assertEqual(self.count("ae_companies"), 3, "ids 1 and 2 are still taken in by a normal run")

    def test_the_contact_first_name_and_company_industry_are_kept(self):
        self.crm.company(1, industry="Textile manufacturing")
        self.crm.contact(1, "ceo@acme.example.com", "CEO", first_name="Asha")
        self.crm.company(2, report="generated", industry="Plastics")
        self.run_intake()
        with db.get_cursor() as cur:
            cur.execute("SELECT first_name FROM ae_contacts WHERE company_id = 1")
            self.assertEqual(cur.fetchone()["first_name"], "Asha")
            cur.execute("SELECT company_id, industry FROM ae_companies ORDER BY company_id")
            self.assertEqual([tuple(r.values()) for r in cur.fetchall()], [(1, "Textile manufacturing"), (2, "Plastics")])

    def test_backfill_fills_missing_first_names_and_industries_without_overwriting(self):
        self.crm.company(1, industry="Textiles")
        self.crm.contact(1, "a@acme.example.com", first_name="Asha")
        self.crm.contact(1, "b@acme.example.com", first_name="Ravi")
        self.run_intake()
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_contacts SET first_name = NULL WHERE email = 'a@acme.example.com'")
            cur.execute("UPDATE ae_contacts SET first_name = 'Kept' WHERE email = 'b@acme.example.com'")
            cur.execute("UPDATE ae_companies SET industry = NULL")
        self.crm.companies[1]["industry"] = "Textiles (new)"
        filled = intake.backfill_details(self.crm)
        with db.get_cursor() as cur:
            cur.execute("SELECT email, first_name FROM ae_contacts ORDER BY email")
            self.assertEqual([tuple(r.values()) for r in cur.fetchall()],
                             [("a@acme.example.com", "Asha"), ("b@acme.example.com", "Kept")])
            cur.execute("SELECT industry FROM ae_companies")
            self.assertEqual(cur.fetchone()["industry"], "Textiles (new)")
        self.assertEqual(filled, {"first_names": 1, "industries": 1})
        self.assertEqual(intake.backfill_details(self.crm), {"first_names": 0, "industries": 0})

    def test_backfill_reads_the_crm_and_writes_only_postgres(self):
        self.crm.company(1)
        self.run_intake()
        w, patch = writer()
        with patch, crm_with(FakeCrmConn()):
            intake.backfill_details(self.crm)
        self.assertEqual(w.calls, [])

    def test_the_raw_website_and_company_scale_are_kept(self):
        self.crm.company(1, website="https://www.Acme.in/x", scale="1-50")
        self.run_intake()
        with db.get_cursor() as cur:
            cur.execute("SELECT website, company_scale FROM ae_companies WHERE company_id = 1")
            self.assertEqual(dict(cur.fetchone()), {"website": "https://www.Acme.in/x", "company_scale": "1-50"})

    def test_a_company_with_a_report_status_is_imported_once_as_legacy(self):
        for i, rs in enumerate(("generated", "no_gap", "unscannable", "failed"), start=1):
            self.crm.company(i, scrape="done", report=rs)
            self.crm.contact(i, f"a@c{i}.example.com")
        counts = self.run_intake()
        for i, rs in enumerate(("generated", "no_gap", "unscannable", "failed"), start=1):
            st, reason = self.status_of(i)
            self.assertEqual(st, "legacy")
            self.assertIn(rs, reason)
        self.assertEqual(counts["legacy_new"], 4)
        self.assertEqual(counts["legacy_by_report_status"],
                         {"generated": 1, "no_gap": 1, "unscannable": 1, "failed": 1})
        self.assertEqual(counts["by_status"], {"legacy": 4})

    def test_legacy_companies_get_no_contacts_no_hold_and_no_stage(self):
        self.crm.company(1, scrape="done", report="generated")
        self.crm.contact(1, "a@c1.example.com")
        self.crm.emailed = {1}
        self.run_intake()
        self.assertEqual(self.status_of(1)[0], "legacy", "not held_already_contacted, not pending")
        self.assertEqual(self.count("ae_contacts"), 0)
        self.assertEqual(self.count("ae_drafts"), 0)

    def test_legacy_import_never_writes_to_the_crm(self):
        self.crm.company(1, report="generated")
        self.crm.company(2)
        w, patch = writer()
        with patch, crm_with(FakeCrmConn()) as _:
            self.run_intake()
        self.assertEqual(w.calls, [])

    def test_the_watermark_advances_to_the_highest_id_taken_legacy_included(self):
        self.crm.company(5)
        self.crm.company(9, report="no_gap")
        self.crm.company(12)
        self.run_intake()
        self.assertEqual(intake.get_watermark("intake_prod"), 12)

    def test_the_watermark_path_reads_only_above_it(self):
        self.crm.company(5)
        self.run_intake()
        self.crm.company(8)
        self.run_intake()
        self.assertEqual(self.status_of(8)[0], "pending")

    def test_batches_cover_everything(self):
        for i in range(1, 8):
            self.crm.company(i)
        self.run_intake(batch_size=3)
        self.assertEqual(self.count("ae_companies"), 7)

    def test_limit_caps_the_companies_taken(self):
        for i in range(1, 8):
            self.crm.company(i)
        counts = self.run_intake(limit=4)
        self.assertEqual(self.count("ae_companies"), 4)
        self.assertEqual(counts["companies_new"], 4)

    def test_reconcile_finds_a_company_below_the_watermark(self):
        self.crm.company(10)
        self.crm.company(20)
        self.run_intake()
        self.assertEqual(intake.get_watermark("intake_prod"), 20)
        self.crm.company(15)                                  # appears below the watermark
        self.run_intake()                                     # watermark path
        self.assertEqual(self.status_of(15)[0], None, "watermark intake must not see id 15")
        found = intake.reconcile(self.crm)
        self.assertEqual(found["companies_new"], 1)
        self.assertEqual(self.status_of(15)[0], "pending")

    def test_reconcile_with_nothing_missing_adds_nothing(self):
        self.crm.company(1)
        self.run_intake()
        self.assertEqual(intake.reconcile(self.crm)["companies_new"], 0)

    def test_dry_run_writes_nothing(self):
        self.crm.company(1)
        self.crm.company(2)
        self.crm.company(3, report="generated")
        self.crm.contact(1, "a@c1.example.com")
        self.crm.emailed = {2}
        self.crm.bounced = {"x@y.example.com"}
        counts = self.run_intake(dry_run=True)
        for t in ("ae_companies", "ae_contacts", "ae_suppression"):
            self.assertEqual(self.count(t), 0, t)
        self.assertEqual(self.count("ae_watermark"), 0)
        self.assertEqual(counts["companies_new"], 3)
        self.assertEqual(counts["legacy_new"], 1)
        self.assertEqual(counts["held_already_contacted"], 1)
        self.assertEqual(counts["suppression_new"], 1)
        self.assertEqual(counts["contacts_new"], 1)
        self.assertEqual(counts["by_status"], {"pending": 1, "held_already_contacted": 1, "legacy": 1})


class TestCohort(DbCase):
    """--sample N re-opens N legacy companies as is_test; they run every stage, write nothing to the CRM."""

    def setUp(self):
        super().setUp()
        self.crm = FakeCrm()
        for i in range(1, 11):
            self.crm.company(i, scrape="done", report="generated")
            self.crm.contact(i, f"p{i}@c{i}.example.com")
        self.crm.emailed = set(range(1, 11))
        intake.run_intake(self.crm)                          # imports all ten as legacy

    def sample(self, n, **kw):
        return intake.run_intake(self.crm, sample=n, **kw)

    def test_sample_n_promotes_n_legacy_companies_to_test(self):
        counts = self.sample(4, seed=1)
        self.assertEqual(self.count("ae_companies", "is_test"), 4)
        self.assertEqual(self.count("ae_companies", "status = 'legacy'"), 6)
        self.assertEqual(counts["cohort"], "test")
        self.assertEqual(counts["companies_new"], 4)

    def test_promoted_companies_are_pending_and_skip_the_already_emailed_hold(self):
        self.sample(4, seed=1)
        self.assertEqual(self.count("ae_companies", "is_test AND status = 'pending'"), 4)
        self.assertEqual(self.count("ae_companies", "status = 'held_already_contacted'"), 0)

    def test_the_test_companies_bring_their_contacts_with_them(self):
        counts = self.sample(3, seed=1)
        self.assertEqual(counts["contacts_new"], 3)
        self.assertEqual(self.count("ae_contacts"), 3)
        self.assertEqual(self.count("ae_contacts c JOIN ae_companies a USING (company_id)", "a.is_test"), 3)

    def test_the_sample_is_reproducible_with_a_seed(self):
        a = intake.pick_sample(3, seed=7)
        b = intake.pick_sample(3, seed=7)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 3)

    def test_a_company_is_not_sampled_twice(self):
        self.sample(4, seed=1)
        self.sample(4, seed=2)
        self.assertEqual(self.count("ae_companies", "is_test"), 8)

    def test_sampling_more_than_exist_takes_what_there_is(self):
        self.sample(50, seed=1)
        self.assertEqual(self.count("ae_companies", "is_test"), 10)

    def test_a_test_sample_never_moves_the_production_watermark(self):
        before = intake.get_watermark("intake_prod")
        self.sample(4, seed=1)
        self.assertEqual(intake.get_watermark("intake_prod"), before)

    def test_a_test_company_stays_a_test_company_when_production_intake_runs_again(self):
        self.sample(4, seed=1)
        intake.run_intake(self.crm)
        self.assertEqual(self.count("ae_companies", "is_test"), 4)

    def test_a_test_company_can_never_be_written_to_the_crm_in_any_column(self):
        self.sample(4, seed=1)
        with db.get_cursor() as cur:
            cur.execute("SELECT company_id FROM ae_companies WHERE is_test ORDER BY company_id")
            ids = [r["company_id"] for r in cur.fetchall()]
        conn = FakeCrmConn(sources={i: "PartnerImport" for i in ids})
        with crm_with(conn):
            for cid in ids:
                for cols in ({"ScrapeStatus": "done"}, {"ReportStatus": "generated"},
                             {"AE_DraftSubject": "hi"}):
                    with self.assertRaises(crm_write.TestCompanyRefused):
                        crm_write.update_company(cid, **cols)
        self.assertEqual(conn.sent, [])

    def test_a_promoted_company_runs_the_whole_pipeline_with_zero_crm_writes(self):
        self.sample(1, seed=1)
        with db.get_cursor() as cur:
            cur.execute("SELECT company_id FROM ae_companies WHERE is_test")
            cid = cur.fetchone()["company_id"]
        conn = FakeCrmConn(sources={cid: "PartnerImport"})
        path = ["checked", "scanned", "gate_passed", "reported", "contact_ranked", "verified",
                "drafted", "written_to_crm", "sent"]
        with crm_with(conn):
            for nxt in path:
                with db.get_cursor() as cur:
                    status.set_status(cur, cid, nxt, f"-> {nxt}")
        self.assertEqual(self.status_of(cid)[0], "sent")
        self.assertEqual(conn.sent, [])

    def test_no_legacy_rows_yet_means_an_empty_sample(self):
        with db.get_cursor() as cur:
            cur.execute("TRUNCATE ae_companies CASCADE")
        counts = self.sample(5, seed=1)
        self.assertEqual(counts["companies_new"], 0)


class CohortsInADryRun(DbCase):
    """Production, legacy and test are counted separately before anything is written."""

    def test_the_three_cohorts_are_reported_apart(self):
        crm = FakeCrm()
        for i in range(1, 6):
            crm.company(i, scrape="done", report="generated")      # legacy
            crm.contact(i, f"p{i}@c{i}.example.com")
        crm.company(6)
        crm.company(7, website="")
        crm.contact(6, "a@acme6.example.com")
        crm.emailed = {1, 2, 3, 4, 5}
        prod = intake.run_intake(crm, dry_run=True)
        test = intake.run_intake(crm, sample=3, seed=1, dry_run=True)
        self.assertEqual(prod["cohort"], "prod")
        self.assertEqual(prod["companies_new"], 7)
        self.assertEqual(prod["legacy_new"], 5)
        self.assertEqual(prod["by_status"], {"legacy": 5, "pending": 2})
        self.assertEqual(test["cohort"], "test")
        self.assertEqual(test["companies_new"], 3)
        self.assertEqual(test["by_status"], {"pending": 3})
        self.assertEqual(test["contacts_new"], 3)
        self.assertEqual(self.count("ae_companies"), 0, "a dry run writes nothing")


class ReportBuildIsRetired(unittest.TestCase):
    """PartnerImport belongs to the campaign pipeline; the old script must not run."""

    SCRIPT = ROOT / "report_build.py"

    def run_script(self, *args):
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"}
        return subprocess.run([sys.executable, str(self.SCRIPT), *args], capture_output=True,
                              text=True, timeout=30, env=env, cwd=str(ROOT))

    def test_it_refuses_to_run_with_a_clear_message(self):
        for args in ((), ("--dry-run", "--limit", "1"), ("--id", "6570"), ("--preflight",),
                     ("--retry-failed",), ("--scan-one", "{}"), ("--crm-write", "{}")):
            r = self.run_script(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("PartnerImport", r.stderr, args)
            self.assertIn("campaign pipeline", r.stderr, args)
            self.assertNotIn("Traceback", r.stderr, args)
            self.assertEqual(r.stdout, "", args)

    def test_it_fires_before_it_imports_anything_or_connects_to_anything(self):
        tree = ast.parse(self.SCRIPT.read_text())
        body = [n for n in tree.body
                if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
        guard = body[0]
        self.assertIsInstance(guard, ast.If, "the first statement after the docstring must be the guard")
        self.assertIn("__main__", ast.dump(guard.test))
        self.assertIn("SystemExit", ast.dump(guard))

    def test_importing_the_module_still_works_for_the_tests_that_use_it(self):
        guard = ast.parse(self.SCRIPT.read_text()).body[1]
        self.assertIn("__main__", ast.dump(guard.test), "guard must not fire on import")


class Migration(DbCase):
    def test_all_the_tables_exist(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'ae\\_%'")
            names = {r["table_name"] for r in cur.fetchall()}
        self.assertEqual(names, {"ae_companies", "ae_contacts", "ae_drafts", "ae_suppression",
                                 "ae_verification_cache", "ae_title_cache", "ae_flags", "ae_watermark",
                                 "ae_mx_cache", "ae_neverbounce_usage"})

    def test_the_spec_columns_exist(self):
        want = {
            "ae_companies": "company_id is_test domain report_ref score band gap_count report_date "
                            "status status_reason scan_attempts next_retry_at created_at updated_at",
            "ae_contacts": "contact_id company_id email job_title rank rank_reason nb_verdict "
                           "nb_decision nb_checked_at status",
            "ae_drafts": "company_id contact_id to_email subject body_html band draft_status "
                         "template_version prompt_version generated_at written_to_crm_at sent_at",
            "ae_suppression": "email reason source created_at",
            "ae_verification_cache": "email verdict checked_at",
            "ae_title_cache": "normalized_title function seniority relevance source prompt_version",
            "ae_flags": "flag value",
            "ae_watermark": "job_name last_crm_id"}
        with db.get_cursor() as cur:
            for table, cols in want.items():
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                            (table,))
                have = {r["column_name"] for r in cur.fetchall()}
                self.assertTrue(set(cols.split()) <= have, f"{table} missing {set(cols.split()) - have}")

    def test_unique_keys(self):
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_companies (company_id) VALUES (1)")
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES ('a@b.co','x','y')")
        for sql in ("INSERT INTO ae_companies (company_id) VALUES (1)",
                    "INSERT INTO ae_suppression (email, reason, source) VALUES ('a@b.co','x','y')"):
            with self.assertRaises(psycopg2.errors.UniqueViolation):
                with db.get_cursor() as cur:
                    cur.execute(sql)

    def test_every_status_in_code_is_accepted_by_the_database_and_nothing_else(self):
        for i, s in enumerate(status.STATUSES):
            self.add_company(1000 + i, s)             # raises CheckViolation if the SQL list lags the code
        with db.get_cursor() as cur:
            cur.execute("""SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint
                           WHERE conrelid = 'ae_companies'::regclass AND contype = 'c'
                             AND pg_get_constraintdef(oid) LIKE '%status%'""")
            listed = set(re.findall(r"'([a-z_]+)'::text", " ".join(r["d"] for r in cur.fetchall())))
        self.assertEqual(listed - {"A", "B", "C", "D"}, set(status.STATUSES))

    def test_nb_decision_is_constrained(self):
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_companies (company_id) VALUES (1)")
        with self.assertRaises(psycopg2.errors.CheckViolation):
            with db.get_cursor() as cur:
                cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, nb_decision)
                               VALUES ('c1', 1, 'a@b.co', 'MAYBE')""")

    def test_migration_does_not_touch_existing_tables(self):
        src = MIGRATION.read_text().upper()
        for risky in ("ALTER TABLE COMPANIES", "DROP TABLE", "ALTER TABLE REPORT", "REFERENCES REPORT"):
            self.assertNotIn(risky, src)


if __name__ == "__main__":
    unittest.main()
