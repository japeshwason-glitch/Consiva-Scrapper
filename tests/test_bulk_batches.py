"""scripts/bulk_batches.py: the pieces that decide when it runs, pauses and stops (no containers involved)."""
import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bulk_batches", ROOT / "scripts" / "bulk_batches.py")
bb = importlib.util.module_from_spec(spec)
sys.modules["bulk_batches"] = bb
spec.loader.exec_module(bb)


def at(h, m):
    return datetime(2026, 10, 16, h, m, tzinfo=bb.IST)


class Pause(unittest.TestCase):
    def test_the_window_brackets_the_0630_daily_job(self):
        self.assertFalse(bb.in_pause_window(at(6, 4)))
        self.assertTrue(bb.in_pause_window(at(6, 5)))
        self.assertTrue(bb.in_pause_window(at(6, 30)))
        self.assertTrue(bb.in_pause_window(at(6, 39)))
        self.assertFalse(bb.in_pause_window(at(6, 40)))

    def test_other_times_are_not_paused(self):
        for h, m in ((0, 0), (2, 0), (12, 0), (23, 59)):
            self.assertFalse(bb.in_pause_window(at(h, m)))


class Plan(unittest.TestCase):
    def test_goodfirms_runs_before_clutch(self):
        self.assertEqual(bb.BATCHES, ["goodfirms", "clutch"])

    def test_only_working_statuses_count_as_work_left(self):
        counts = {"pending": 2, "checked": 1, "reported": 3, "written_to_crm": 50, "scan_failed": 9,
                  "skipped_invalid": 7, "no_gaps": 4, "score_too_high": 1}
        self.assertEqual(bb.work_left(counts), 6)
        self.assertEqual(bb.work_left({"written_to_crm": 10}), 0)

    def test_every_working_status_is_a_real_non_final_status(self):
        from campaign import status
        for s in bb.WORKING:
            self.assertIn(s, status.TRANSITIONS)
            self.assertTrue(status.TRANSITIONS[s], f"{s} is final")

    def test_the_batches_are_known_to_the_pipeline_and_are_express(self):
        from campaign import settings
        for b in bb.BATCHES:
            self.assertIn(b, settings.BATCHES)
            self.assertIn(b, settings.EXPRESS_BATCHES)

    def test_the_run_command_allows_role_addresses_and_writes_the_crm_for_one_batch_only(self):
        self.assertIn("AE_ROLE_LOCAL_PARTS=__none__", bb.ENV)
        self.assertEqual(bb.LIMIT, 100)


class Choice(unittest.TestCase):
    def test_default_is_both_goodfirms_first(self):
        self.assertEqual(bb.pick_batches(None), ["goodfirms", "clutch"])
        self.assertEqual(bb.pick_batches(""), ["goodfirms", "clutch"])

    def test_clutch_alone(self):
        self.assertEqual(bb.pick_batches("clutch"), ["clutch"])

    def test_the_order_given_is_kept(self):
        self.assertEqual(bb.pick_batches("clutch, goodfirms"), ["clutch", "goodfirms"])

    def test_an_unknown_batch_is_refused(self):
        with self.assertRaises(SystemExit):
            bb.pick_batches("clutch,nope")


class Records(unittest.TestCase):
    def test_rows_are_appended_with_one_header(self):
        import csv
        import tempfile
        rows = [{"company_id": 1, "company_name": "Acme, Inc", "domain": "acme.example.com", "status": "reported",
                 "report_url": "https://r/x.pdf", "ignored_extra": "x"},
                {"company_id": 2, "company_name": "Beta", "domain": "beta.example.com", "status": "no_gaps"}]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "records.csv"
            self.assertEqual(bb.write_records("goodfirms", rows, path), 2)
            self.assertEqual(bb.write_records("clutch", rows[:1], path), 1)
            got = list(csv.DictReader(path.open()))
        self.assertEqual([r["company_id"] for r in got], ["1", "2", "1"])
        self.assertEqual([r["batch"] for r in got], ["goodfirms", "goodfirms", "clutch"])
        self.assertEqual(got[0]["company_name"], "Acme, Inc")              # a comma in a name survives
        self.assertEqual(list(got[0].keys()), bb.RECORD_COLUMNS)
        self.assertEqual(got[1]["band"], "")

    def test_nothing_to_record_writes_no_file_noise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "records.csv"
            self.assertEqual(bb.write_records("goodfirms", [], path), 0)


if __name__ == "__main__":
    unittest.main()
