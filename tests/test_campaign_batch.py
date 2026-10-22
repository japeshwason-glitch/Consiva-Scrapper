"""Held-out batches: the scheduler (and any run without --batch) never sees a batch company, and vice versa."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import crm_read, domains, intake, jobs, report_stage, settings, stages, status
from tests.test_campaign import DbCase, FakeCrm

BATCH = "gf_clutch"


class RecordingConn:
    """A SQL Server connection that records the SELECTs sent and returns no rows."""

    def __init__(self):
        self.sent = []

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql, params=()):
                conn.sent.append((sql, tuple(params)))

            def fetchall(self):
                return []

        return Cur()

    def close(self):
        pass


class ScopeSql(unittest.TestCase):
    def test_a_normal_run_sees_only_untagged_companies(self):
        self.assertEqual(status.scope_sql(), (" AND batch IS NULL", []))

    def test_a_batch_run_sees_only_its_batch(self):
        with status.in_batch(BATCH):
            self.assertEqual(status.scope_sql(), (" AND batch = %s", [BATCH]))
        self.assertEqual(status.scope_sql(), (" AND batch IS NULL", []))

    def test_ids_narrow_the_batch_further(self):
        with status.in_batch(BATCH), status.only_companies([3, 1]):
            sql, params = status.scope_sql()
        self.assertEqual(sql, " AND batch = %s AND company_id = ANY(%s)")
        self.assertEqual(params, [BATCH, [1, 3]])

    def test_the_default_batch_holds_out_goodfirms_and_clutch(self):
        self.assertEqual(settings.BATCHES[BATCH], ("goodfirms", "clutch"))
        self.assertEqual(settings.HELD_OUT_SOURCES, ("clutch", "goodfirms"))

    def test_batches_parse_from_the_environment_format(self):
        self.assertEqual(settings._parse_batches("a=X, y;b=z"), {"a": ("x", "y"), "b": ("z",)})
        self.assertEqual(settings._parse_batches(""), {})


class ReaderSources(unittest.TestCase):
    def sql(self, batch=None):
        conn = RecordingConn()
        crm_read.CrmReader(conn=conn, batch=batch).companies_after(0)
        return conn.sent[-1]

    def test_the_default_reader_never_reads_a_held_out_source(self):
        sql, params = self.sql()
        self.assertIn("IS NULL OR LOWER(", sql)
        self.assertIn("NOT IN", sql)
        self.assertEqual(list(params[:2]), ["clutch", "goodfirms"])

    def test_the_default_reader_still_reads_a_null_source(self):
        self.assertIn("IS NULL", self.sql()[0])

    def test_explicitly_allowing_a_held_out_source_does_not_let_it_through(self):
        with mock.patch.object(settings, "ALLOWED_SOURCES", ("goodfirms", "PartnerImport")):
            sql, params = self.sql()
        self.assertIn("NOT IN", sql)
        self.assertEqual(list(params[:2]), ["goodfirms", "PartnerImport"])
        self.assertEqual(list(params[2:4]), ["clutch", "goodfirms"])

    def test_a_batch_reader_reads_only_its_sources_case_insensitively(self):
        sql, params = self.sql(BATCH)
        self.assertIn("LOWER(", sql)
        self.assertNotIn("NOT IN", sql)
        self.assertEqual(list(params[:2]), ["goodfirms", "clutch"])

    def test_an_unknown_batch_is_refused(self):
        with self.assertRaises(ValueError):
            crm_read.CrmReader(conn=RecordingConn(), batch="nope")


class Selection(DbCase):
    """Every stage's own selection goes through scope_sql: a batch company is invisible outside its batch."""

    def tag(self, company_id, batch):
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET batch = %s WHERE company_id = %s", (batch, company_id))

    def setUp(self):
        super().setUp()
        for i, st in ((1, "pending"), (2, "pending")):
            self.add_company(i, st)
        self.tag(2, BATCH)

    def claimed(self, st="pending"):
        with status.claim(st, 10) as (cur, rows):
            return sorted(r["company_id"] for r in rows)

    def test_claim_skips_a_batch_company_outside_its_batch(self):
        self.assertEqual(self.claimed(), [1])

    def test_claim_sees_only_the_batch_inside_it(self):
        with status.in_batch(BATCH):
            self.assertEqual(self.claimed(), [2])

    def test_the_report_stage_selection_is_scoped(self):
        for i in (1, 2):
            self.add_company(i + 10, "gate_passed")
        self.tag(12, BATCH)
        self.assertEqual([r["company_id"] for r in report_stage._candidates(10, False)], [11])
        with status.in_batch(BATCH):
            self.assertEqual([r["company_id"] for r in report_stage._candidates(10, False)], [12])

    def test_the_domain_stage_selection_is_scoped(self):
        for i in (1, 2):
            self.add_company(i + 20, "checked")
        self.tag(22, BATCH)
        with domains._claim_unresolved(10, False) as (cur, rows):
            self.assertEqual([r["company_id"] for r in rows], [21])
        with status.in_batch(BATCH), domains._claim_unresolved(10, False) as (cur, rows):
            self.assertEqual([r["company_id"] for r in rows], [22])

    def test_the_funnel_counts_only_the_active_batch(self):
        self.assertEqual(jobs.funnel(False), {"pending": 1})
        with status.in_batch(BATCH):
            self.assertEqual(jobs.funnel(False), {"pending": 1})
            self.add_company(5, "reported")
            self.tag(5, BATCH)
            self.assertEqual(jobs.funnel(False), {"pending": 1, "reported": 1})
        self.assertEqual(jobs.funnel(False), {"pending": 1})


class BatchIntake(DbCase):
    def batch_of(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("SELECT batch FROM ae_companies WHERE company_id = %s", (company_id,))
            return cur.fetchone()["batch"]

    def crm(self, batch=None):
        crm = FakeCrm()
        crm.batch = batch
        for i in (1, 2, 3):
            crm.company(i)
            crm.contact(i, f"p{i}@c{i}.example.com")
        return crm

    def test_a_batch_intake_tags_what_it_takes_in(self):
        intake.run_intake(self.crm(BATCH), batch_name=BATCH)
        self.assertEqual([self.batch_of(i) for i in (1, 2, 3)], [BATCH] * 3)

    def test_a_normal_intake_leaves_companies_untagged(self):
        intake.run_intake(self.crm())
        self.assertEqual([self.batch_of(i) for i in (1, 2, 3)], [None] * 3)

    def test_the_batch_keeps_its_own_watermark(self):
        intake.run_intake(self.crm(BATCH), batch_name=BATCH)
        self.assertEqual(intake.get_watermark(f"{intake.WATERMARK_JOB}:{BATCH}"), 3)
        self.assertEqual(intake.get_watermark(intake.WATERMARK_JOB), 0)

    def test_a_batch_intake_is_idempotent(self):
        intake.run_intake(self.crm(BATCH), batch_name=BATCH)
        again = intake.run_intake(self.crm(BATCH), batch_name=BATCH)
        self.assertEqual(again["companies_new"], 0)
        self.assertEqual(self.count("ae_companies"), 3)

    def test_a_reader_of_another_batch_is_refused(self):
        with self.assertRaises(ValueError):
            intake.run_intake(self.crm(None), batch_name=BATCH)

    def test_backfill_touches_only_the_readers_own_batch(self):
        crm = self.crm(BATCH)
        intake.run_intake(crm, batch_name=BATCH)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_contacts SET first_name = NULL")
        self.assertEqual(intake.backfill_details(self.crm(None))["first_names"], 0)   # a normal reader: not its rows
        self.assertEqual(intake.backfill_details(crm)["first_names"], 3)


class PrepareBatch(DbCase):
    def run_prepare(self, crm, **kw):
        seen = []

        def fn(**k):
            seen.append(status.scope_sql())
            return {}

        table = tuple(stages.Stage(n, f"s{n}", "", (), fn) for n in range(1, 11))
        with mock.patch.object(stages, "STAGES", table):
            jobs.prepare(reader=crm, production=True, **kw)
        return seen

    def test_a_batch_prepare_runs_every_stage_inside_the_batch(self):
        crm = FakeCrm()
        crm.batch = BATCH
        seen = self.run_prepare(crm, batch=BATCH)
        self.assertTrue(seen)
        self.assertTrue(all(s == (" AND batch = %s", [BATCH]) for s in seen), seen)

    def test_a_plain_prepare_runs_every_stage_outside_any_batch(self):
        seen = self.run_prepare(FakeCrm())
        self.assertTrue(seen)
        self.assertTrue(all(s == (" AND batch IS NULL", []) for s in seen), seen)

    def test_last_stage_stops_the_run_and_skips_the_ae_write(self):
        crm = FakeCrm()
        crm.batch = BATCH
        calls = []

        def fn(n):
            return lambda **k: calls.append(n) or {}

        table = tuple(stages.Stage(n, f"s{n}", "", (), fn(n)) for n in range(1, 11))
        with mock.patch.object(stages, "STAGES", table):
            jobs.prepare(reader=crm, production=True, batch=BATCH, last_stage=6, write_ae=True)
        self.assertEqual(calls, [2, 3, 4, 5, 6])

    def test_last_stage_out_of_range_is_refused(self):
        for bad in (1, 10):
            with self.assertRaises(SystemExit):
                jobs.prepare(reader=FakeCrm(), last_stage=bad)

    def test_goodfirms_and_clutch_can_each_be_run_alone(self):
        self.assertEqual(settings.BATCHES["goodfirms"], ("goodfirms",))
        self.assertEqual(settings.BATCHES["clutch"], ("clutch",))
        self.assertEqual(settings.HELD_OUT_SOURCES, ("clutch", "goodfirms"))

    def test_a_batch_cannot_be_combined_with_a_sample(self):
        with self.assertRaises(SystemExit):
            jobs.prepare(reader=FakeCrm(), batch=BATCH, sample=3)

    def test_an_unknown_batch_is_refused(self):
        with self.assertRaises(SystemExit):
            jobs.prepare(reader=FakeCrm(), batch="nope")


if __name__ == "__main__":
    unittest.main()
