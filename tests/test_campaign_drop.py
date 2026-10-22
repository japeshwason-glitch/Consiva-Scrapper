"""drop_drafts: a draft leaves circulation as `expired`; a sent one is never touched."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import crm_write, drop_drafts
from tests.test_campaign import DbCase


class Writer:
    def __init__(self, sent=(), fail=None):
        self.calls, self.sent, self.fail = [], set(sent), fail

    def __call__(self, company_id, only_if_unsent=False, allow_held_out=False, **columns):
        if self.fail:
            raise self.fail
        self.calls.append((company_id, only_if_unsent, allow_held_out, columns))
        return company_id not in self.sent


class Drop(DbCase):
    def draft(self, cid, status_="written_to_crm", is_test=False):
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_companies (company_id, is_test, domain, status) VALUES (%s, %s, %s, %s)",
                        (cid, is_test, f"c{cid}.example.com", status_))
            cur.execute("""INSERT INTO ae_drafts (company_id, contact_id, to_email, subject, body_html, band, draft_status, template_version)
                           VALUES (%s, 'ct', 'a@b.co', 'S', '<p>x</p>', 'B', 'ready', 'V2-1.0-aaaaaaaa')""", (cid,))

    def state(self, cid):
        with db.get_cursor() as cur:
            cur.execute("SELECT a.status, a.status_reason, d.draft_status FROM ae_companies a JOIN ae_drafts d USING (company_id) WHERE a.company_id = %s", (cid,))
            return cur.fetchone()

    def go(self, ids, writer, **kw):
        with mock.patch.object(crm_write, "update_company", writer):
            return drop_drafts.drop(ids, "junk company name", log=lambda *_: None, **kw)

    def test_a_written_draft_is_expired_in_postgres_and_in_the_crm(self):
        self.draft(1)
        w = Writer()
        self.assertEqual(self.go([1], w), {1: "dropped_in_crm"})
        s = self.state(1)
        self.assertEqual((s["status"], s["draft_status"], s["status_reason"]), ("expired", "expired", "junk company name"))
        self.assertEqual(w.calls, [(1, True, True, {"AE_DraftStatus": "expired"})])

    def test_a_draft_that_was_never_written_changes_only_postgres(self):
        self.draft(1, "drafted")
        w = Writer()
        self.assertEqual(self.go([1], w), {1: "dropped"})
        self.assertEqual((self.state(1)["status"], w.calls), ("expired", []))

    def test_a_draft_sent_in_the_meantime_is_left_exactly_as_it_was(self):
        self.draft(1)
        self.assertEqual(self.go([1], Writer(sent=[1])), {1: "skipped_sent"})
        s = self.state(1)
        self.assertEqual((s["status"], s["draft_status"]), ("written_to_crm", "ready"))

    def test_a_failed_crm_write_changes_nothing(self):
        self.draft(1)
        with self.assertRaises(crm_write.CrmWriteFailed):
            self.go([1], Writer(fail=crm_write.CrmWriteFailed("boom")))
        s = self.state(1)
        self.assertEqual((s["status"], s["draft_status"]), ("written_to_crm", "ready"))

    def test_test_companies_other_statuses_and_unknown_ids_are_skipped(self):
        self.draft(1, is_test=True)
        self.draft(2, "verified")
        w = Writer()
        self.assertEqual(self.go([1, 2, 99], w), {1: "skipped_test", 2: "skipped_status_verified", 99: "skipped_unknown"})
        self.assertEqual(w.calls, [])

    def test_a_dry_run_changes_nothing(self):
        self.draft(1)
        w = Writer()
        self.assertEqual(self.go([1], w, dry_run=True), {1: "would_drop_in_crm"})
        self.assertEqual((self.state(1)["status"], w.calls), ("written_to_crm", []))


if __name__ == "__main__":
    unittest.main()
