"""The prepare job: intake, then stages 2-8 for the test cohort; production only when enabled."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import jobs, settings, stages
from tests.test_campaign import DbCase, FakeCrm


class Recorder:
    def __init__(self):
        self.calls = []

    def stage(self, number, name, implemented=True):
        def fn(**kw):
            self.calls.append((number, kw.get("only_test"), kw.get("dry_run"), kw.get("limit")))
            return {"stage": number}
        return stages.Stage(number, name, "", (), fn if implemented else None)


class Prepare(DbCase):
    def setUp(self):
        super().setUp()
        self.rec = Recorder()
        self.table = tuple(self.rec.stage(n, f"s{n}", implemented=n != 9) for n in range(1, 11))
        self.crm = FakeCrm()
        for i in range(1, 6):
            self.crm.company(i, report="generated")
            self.crm.contact(i, f"p{i}@c{i}.example.com")

    def prepare(self, **kw):
        with mock.patch.object(stages, "STAGES", self.table):
            return jobs.prepare(reader=self.crm, **kw)

    def test_a_sample_runs_stages_2_to_8_in_order_for_the_test_cohort_only(self):
        self.prepare(sample=3, seed=1)
        self.assertEqual([c[0] for c in self.rec.calls], [2, 3, 4, 5, 6, 7, 8])
        self.assertTrue(all(c[1] is True for c in self.rec.calls), "only the test cohort")

    def test_the_draft_stage_is_never_run(self):
        self.prepare(sample=3, seed=1)
        self.assertNotIn(9, [c[0] for c in self.rec.calls])

    def test_intake_runs_first_so_the_sample_has_legacy_rows_to_draw_from(self):
        result = self.prepare(sample=3, seed=1)
        self.assertEqual([r["cohort"] for r in result["intake"]], ["prod", "test"])
        self.assertEqual(self.count("ae_companies", "is_test"), 3)

    def test_without_a_sample_or_the_production_switch_no_stage_runs(self):
        self.prepare()
        self.assertEqual(self.rec.calls, [])
        self.assertEqual(self.count("ae_companies"), 5, "intake still ran")

    def test_the_production_switch_runs_production_stages(self):
        with mock.patch.object(settings, "PRODUCTION_ENABLED", True):
            self.prepare()
        self.assertEqual([c[1] for c in self.rec.calls], [False] * 7)

    def test_the_production_flag_works_without_the_switch(self):
        self.prepare(production=True)
        self.assertEqual([c[1] for c in self.rec.calls], [False] * 7)

    def test_a_sample_with_production_runs_both_cohorts(self):
        self.prepare(sample=2, seed=1, production=True)
        self.assertEqual({c[1] for c in self.rec.calls}, {True, False})

    def test_dry_run_passes_dry_run_to_every_stage_and_writes_nothing(self):
        self.prepare(sample=3, seed=1, dry_run=True)
        self.assertTrue(all(c[2] is True for c in self.rec.calls))
        self.assertEqual(self.count("ae_companies"), 0)

    def test_limit_is_passed_down(self):
        self.prepare(sample=3, seed=1, limit=7)
        self.assertTrue(all(c[3] == 7 for c in self.rec.calls))

    def test_the_funnel_is_recorded_after_each_stage(self):
        result = self.prepare(sample=3, seed=1)
        steps = result["stages"]["test"]
        self.assertEqual([s["stage"] for s in steps], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(steps[0]["funnel"], {"pending": 3})

    def test_a_dry_run_with_ids_names_the_companies_each_stage_would_touch_and_only_those(self):
        for i in (7, 8, 9):
            self.crm.company(i)
        result = self.prepare(production=True, ids=[7, 9], dry_run=True, write_ae=True)
        plan = result["dry_run_plan"]
        self.assertEqual(plan["scope"], [7, 9])
        self.assertEqual(plan["intake_would_take"], [7, 9])
        self.assertEqual([p["stage"] for p in plan["stages"]], [2, 3, 4, 5, 6, 7, 8, 9, 10])
        for p in plan["stages"]:
            self.assertEqual(p["would_touch"], [7, 9], p)
        self.assertNotIn(8, [i for p in plan["stages"] for i in p["would_touch"]])
        self.assertEqual(self.count("ae_companies"), 0, "a dry run writes nothing")

    def test_the_funnel_in_a_scoped_run_counts_only_the_named_companies(self):
        from campaign import status
        self.add_company(50, "no_gaps")
        self.add_company(51, "score_too_high")
        self.add_company(7, "checked")
        self.assertEqual(jobs.funnel(False), {"no_gaps": 1, "score_too_high": 1, "checked": 1})
        with status.only_companies([7]):
            self.assertEqual(jobs.funnel(False), {"checked": 1})

    def test_a_dry_run_plan_leaves_out_the_write_stage_without_write_ae(self):
        self.crm.company(7)
        result = self.prepare(production=True, ids=[7], dry_run=True)
        self.assertNotIn(10, [p["stage"] for p in result["dry_run_plan"]["stages"]])

    def test_a_dry_run_without_ids_has_no_plan(self):
        self.assertNotIn("dry_run_plan", self.prepare(sample=2, seed=1, dry_run=True))

    def test_cron_is_safe_by_default(self):
        self.assertFalse(settings.PRODUCTION_ENABLED)
        self.assertNotIn("--production", (ROOT / "campaign" / "crontab").read_text())


if __name__ == "__main__":
    unittest.main()
