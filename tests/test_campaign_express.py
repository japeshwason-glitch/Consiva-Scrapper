"""Express batches: no contact ranking, no NeverBounce; stage 9 drafts on an address that was not verified, for
GoodFirms and Clutch companies only."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db

from campaign import draft_templates, drafting, express, intake, ranking, settings, status, verify
from tests.test_campaign import DbCase, FakeCrm
from tests.test_campaign_draft import DraftCase

BATCH = "goodfirms"


class Rows(DbCase):
    def company(self, cid, st="reported", batch=BATCH):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, company_name, batch)
                           VALUES (%s, FALSE, %s, %s, %s, %s)""", (cid, f"c{cid}.example.com", st, f"Company {cid}", batch))

    def contact(self, cid, n, email=None, passed=True, primary=None, rank=None):
        with db.get_cursor() as cur:
            cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, first_name, check_passed, is_primary, rank)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (f"ct-{cid}-{n}", cid, email or f"info{n}@c{cid}.example.com", f"Company {cid}", passed, primary, rank))

    def row(self, table, where, params=()):
        with db.get_cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE {where}", params)
            return cur.fetchall()


class ExpressRank(Rows):
    def rank(self):
        with status.in_batch(BATCH):
            return ranking.run()

    def test_the_primary_contact_is_chosen_even_when_it_is_not_the_first(self):
        self.company(1)
        self.contact(1, 1, primary=False)
        self.contact(1, 2, primary=True)
        counts = self.rank()
        self.assertEqual(counts["contact_ranked"], 1)
        self.assertTrue(counts["express"])
        ranks = {r["contact_id"]: r["rank"] for r in self.row("ae_contacts", "company_id = 1")}
        self.assertEqual(ranks, {"ct-1-2": 1, "ct-1-1": 2})
        self.assertEqual(self.status_of(1)[0], "contact_ranked")

    def test_with_no_flag_the_first_contact_is_chosen(self):
        self.company(1)
        self.contact(1, 1)
        self.contact(1, 2)
        self.rank()
        self.assertEqual({r["contact_id"]: r["rank"] for r in self.row("ae_contacts", "company_id = 1")},
                         {"ct-1-1": 1, "ct-1-2": 2})

    def test_a_contact_that_failed_the_free_checks_is_never_chosen(self):
        self.company(1)
        self.contact(1, 1, passed=False, primary=True)
        self.contact(1, 2, passed=True, primary=False)
        self.rank()
        ranks = {r["contact_id"]: r["rank"] for r in self.row("ae_contacts", "company_id = 1")}
        self.assertEqual(ranks["ct-1-2"], 1)
        self.assertIsNone(ranks["ct-1-1"])

    def test_no_passing_contact_holds_the_company(self):
        self.company(1)
        self.contact(1, 1, passed=False)
        self.assertEqual(self.rank()["held_no_contact"], 1)
        self.assertEqual(self.status_of(1)[0], "held_no_contact")

    def test_no_model_is_called(self):
        self.company(1)
        self.contact(1, 1)
        with mock.patch("campaign.title_llm.classify", side_effect=AssertionError("the model must not be called"), create=True):
            self.rank()

    def test_a_run_outside_the_batch_never_sees_the_company(self):
        self.company(1)
        self.contact(1, 1)
        counts = ranking.run()                                  # the normal stage, as the scheduler runs it
        self.assertEqual(counts["companies_claimed"], 0)
        self.assertEqual(self.status_of(1)[0], "reported")

    def test_a_batch_that_is_not_express_keeps_the_normal_stage(self):
        with mock.patch.object(settings, "EXPRESS_BATCHES", ("other",)):
            self.assertFalse(express.active())
            with status.in_batch(BATCH):
                self.assertFalse(express.active())


class ExpressVerify(Rows):
    def verify(self):
        with status.in_batch(BATCH):
            return verify.run()

    def test_the_chosen_contact_is_recorded_as_not_verified_and_nothing_is_spent(self):
        self.company(1, "contact_ranked")
        self.contact(1, 1, rank=1)
        with mock.patch.object(verify, "_ask", side_effect=AssertionError("NeverBounce must not be asked")):
            counts = self.verify()
        self.assertEqual((counts["verified"], counts["credits_used"], counts["api_calls"]), (1, 0, 0))
        ct = self.row("ae_contacts", "company_id = 1")[0]
        self.assertEqual((ct["nb_source"], ct["nb_decision"], ct["nb_verdict"]), ("skipped", "OK", "not_verified"))
        co = self.row("ae_companies", "company_id = 1")[0]
        self.assertEqual((co["status"], co["chosen_contact_id"], co["risky"]), ("verified", "ct-1-1", False))

    def test_a_suppressed_contact_is_not_verified(self):
        self.company(1, "contact_ranked")
        self.contact(1, 1, email="info1@c1.example.com", rank=1)
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES ('info1@c1.example.com', 'bounced', 'test')")
        self.assertEqual(self.verify()["suppressed"], 1)
        self.assertEqual(self.status_of(1)[0], "suppressed")

    def test_the_normal_stage_does_not_touch_an_express_company(self):
        self.company(1, "contact_ranked")
        self.contact(1, 1, rank=1)
        verify.run()
        self.assertEqual(self.status_of(1)[0], "contact_ranked")


class ExpressDraft(DraftCase):
    def tag(self, cid, batch):
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET batch = %s WHERE company_id = %s", (batch, cid))

    def gate(self, cid):
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM ae_companies WHERE company_id = %s", (cid,))
            co = cur.fetchone()
            cur.execute("SELECT * FROM ae_contacts WHERE contact_id = %s", (co["chosen_contact_id"],))
            return drafting.gate_reason(cur, co, cur.fetchone())

    def test_an_express_company_is_drafted_on_an_unverified_address_as_ready(self):
        self.verified(1, source="skipped", first_name="Company 1")
        self.tag(1, BATCH)
        with status.in_batch(BATCH):
            counts, _ = self.run_stage()
        self.assertEqual(counts["drafted"], 1)
        d = self.draft_row(1)
        self.assertEqual(d["draft_status"], "ready")
        self.assertIn("Hello Company 1 team,", d["body_html"])
        self.assertEqual(self.company_status(1), "drafted")

    def test_the_same_address_is_refused_outside_an_express_batch(self):
        self.verified(1, source="skipped")
        self.assertEqual(self.gate(1), "mock_verdict")
        with status.in_batch(BATCH):
            pass
        counts, _ = self.run_stage()
        self.assertEqual(counts["drafted"], 0)
        self.assertEqual(self.company_status(1), "verified")

    def test_a_batch_that_is_not_express_is_refused_too(self):
        self.verified(1, source="skipped")
        self.tag(1, "partner_x")
        self.assertEqual(self.gate(1), "mock_verdict")

    def test_a_mock_verdict_is_never_usable_even_in_an_express_batch(self):
        self.verified(1, source="mock")
        self.tag(1, BATCH)
        self.assertEqual(self.gate(1), "mock_verdict")

    def test_express_drafts_ignore_the_daily_cap_and_are_not_counted_by_it(self):
        for i in (1, 2, 3):
            self.verified(i, source="skipped")
            self.tag(i, BATCH)
        with mock.patch.object(settings, "DRAFTS_PER_DAY", 1), status.in_batch(BATCH):
            counts, _ = self.run_stage()
        self.assertEqual(counts["drafted"], 3)
        with db.get_cursor() as cur:
            self.assertEqual(drafting.drafts_today(cur), 0)         # the normal pipeline's cap is untouched


class Greeting(unittest.TestCase):
    def render(self, first_name, company="Acme Tech", domain="acme.example.com"):
        return draft_templates.render("B", "", "", "", first_name, "https://r/x.pdf", domain,
                                      {"company": company, "n_tracking": 1, "no_banner": False, "n_third": 0}).html

    def test_the_greeting_is_the_company_even_when_a_first_name_is_known(self):
        html = self.render("Priya")                                 # template v2 greets the company, not a person
        self.assertIn("Hello Acme Tech team,", html)
        self.assertNotIn("Priya", html)

    def test_a_first_name_that_is_the_company_name_is_not_used(self):
        html = self.render("Acme Tech")
        self.assertIn("Hello Acme Tech team,", html)
        self.assertNotIn("Hi Acme Tech", html)

    def test_the_comparison_ignores_case_and_spaces(self):
        self.assertIn("Hello Acme Tech team,", self.render("  acme tech "))

    def test_with_no_name_and_no_company_it_is_plain_hello(self):
        self.assertIn("<p>Hello,</p>", self.render(None, company="acme.example.com"))

    def test_every_band_greets_the_same_way(self):
        for band in "ABCD":
            html = draft_templates.render(band, "", "", "", None, "u", "a.example.com",
                                          {"company": "Acme", "n_tracking": 1, "no_banner": False, "n_third": 0}).html
            self.assertIn("Hello Acme team,", html, band)


class Possessive(unittest.TestCase):
    def test_a_name_ending_in_s_takes_only_an_apostrophe(self):
        self.assertEqual(draft_templates.possessive("CodesClue Technologies"), "CodesClue Technologies\u2019")
        self.assertEqual(draft_templates.possessive("OPENS"), "OPENS\u2019")

    def test_any_other_name_takes_apostrophe_s(self):
        self.assertEqual(draft_templates.possessive("OpenXcell"), "OpenXcell\u2019s")
        self.assertEqual(draft_templates.possessive("Startbit IT Solutions Pvt. Ltd. "), "Startbit IT Solutions Pvt. Ltd.\u2019s")

    def test_every_band_puts_it_in_the_subject(self):
        for band in "ABCD":
            for company, want in (("Codes Technologies", "We scanned Codes Technologies\u2019 website\u2014"),
                                  ("OpenXcell", "We scanned OpenXcell\u2019s website\u2014")):
                subject = draft_templates.render(band, "", "", "", None, "u", "a.example.com",
                                                 {"company": company, "n_tracking": 1, "no_banner": False, "n_third": 0}).subject
                self.assertTrue(subject.startswith(want), (band, subject))

    def test_a_missing_company_name_still_says_your_website(self):
        mail = draft_templates.render("B", "", "", "", None, "u", "a.example.com",
                                      {"company": "a.example.com", "n_tracking": 1, "no_banner": False, "n_third": 0})
        self.assertTrue(mail.subject.startswith("We scanned your website\u2014"))
        self.assertIn("we reviewed a.example.com", mail.html)


class TemplateV2(unittest.TestCase):
    URL = "https://reports.consiva.tech/r/0123456789abcdef0123456789abcdef.pdf"

    def mail(self, company="Acme Tech", **f):
        facts = {"company": company, "n_tracking": 3, "n_third": 2, "no_banner": False, "no_policy": False, **f}
        return draft_templates.render("B", "", "", "", None, self.URL, "acme.example.com", facts)

    def test_the_subject_is_the_approved_line(self):
        self.assertEqual(self.mail().subject, "We scanned Acme Tech\u2019s website\u2014here are the DPDP Privacy gaps we found")

    def test_an_ampersand_in_the_name_is_not_escaped_in_the_subject(self):
        self.assertIn("AT&T\u2019s website", self.mail("AT&T").subject)
        self.assertIn("AT&amp;T", self.mail("AT&T").html)                     # the body is HTML, so it is escaped there

    def test_two_buttons_with_the_right_labels_and_links(self):
        html = self.mail().html
        self.assertEqual(html.count('bgcolor="#1a56db"'), 2)
        self.assertIn('href="https://consiva.ai/" style=', html)
        self.assertIn("Fix My Website&rsquo;s Consent</a>", html)
        self.assertIn(f'<a href="{self.URL}" style=', html)
        self.assertIn(">View your website scan</a>", html)

    def test_request_a_consultation_is_a_text_link_to_the_contact_page(self):
        self.assertIn('<a href="https://consiva.ai/contact">request a consultation</a> with our team', self.mail().html)

    def test_the_fixed_claims_are_in_ascii_entities_so_no_column_can_mangle_them(self):
        html = self.mail().html
        self.assertIn("&#8377;250 crore", html)
        self.assertIn("13 November 2026", html)
        self.assertTrue(all(ord(c) < 128 for c in html))

    def test_counts_are_listed_only_when_above_zero(self):
        self.assertEqual(self.mail(n_tracking=1, n_third=0).text.count("\u2022"), 1)
        self.assertNotIn("third-party", self.mail(n_tracking=4, n_third=0).html)
        self.assertEqual(self.mail().text.count("\u2022"), 2)

    def test_the_unsubscribe_merge_field_is_kept_for_the_crm_tool(self):
        self.assertIn("{{UnsubscribeButton}}", self.mail().html)

    def test_the_plain_text_version_carries_both_buttons_and_the_scan_link(self):
        text = self.mail().text
        self.assertIn("Fix My Website\u2019s Consent (https://consiva.ai/)", text)
        self.assertIn(f"View your website scan ({self.URL})", text)

    def test_the_version_label_is_short_enough_for_the_crm_column(self):
        self.assertLessEqual(len(draft_templates.crm_version(self.mail().template_version)), 20)
        self.assertEqual(draft_templates.crm_version(self.mail().template_version), "V2-1.0")


class JobRunning(DbCase):
    def test_it_sees_a_held_lock_and_never_takes_it(self):
        from campaign import locks
        self.assertFalse(locks.job_running("probe_job"))
        with locks.job_lock("probe_job") as got:
            self.assertTrue(got)
            self.assertTrue(locks.job_running("probe_job"))
            self.assertFalse(locks.job_running("another_job"))
        self.assertFalse(locks.job_running("probe_job"))
        with locks.job_lock("probe_job") as got:             # asking did not leave the lock behind
            self.assertTrue(got)

    def test_asking_does_not_block_a_job_from_taking_its_lock(self):
        from campaign import locks
        for _ in range(3):
            locks.job_running("daily")
        with locks.job_lock("daily") as got:
            self.assertTrue(got)


class PrimaryAtIntake(DbCase):
    def crm(self):
        crm = FakeCrm()
        crm.batch = BATCH
        crm.company(1)
        crm.contact(1, "a@c1.example.com")
        crm.contact(1, "b@c1.example.com")
        crm.contacts_[0]["is_primary"], crm.contacts_[1]["is_primary"] = False, True
        return crm

    def flags(self):
        with db.get_cursor() as cur:
            cur.execute("SELECT email, is_primary FROM ae_contacts ORDER BY email")
            return [(r["email"], r["is_primary"]) for r in cur.fetchall()]

    def test_intake_keeps_the_primary_flag(self):
        intake.run_intake(self.crm(), batch_name=BATCH)
        self.assertEqual(self.flags(), [("a@c1.example.com", False), ("b@c1.example.com", True)])

    def test_backfill_fills_a_missing_flag_without_touching_a_set_one(self):
        crm = self.crm()
        intake.run_intake(crm, batch_name=BATCH)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_contacts SET is_primary = NULL WHERE email = 'b@c1.example.com'")
        self.assertEqual(intake.backfill_details(crm)["primaries"], 1)
        self.assertEqual(self.flags(), [("a@c1.example.com", False), ("b@c1.example.com", True)])


if __name__ == "__main__":
    unittest.main()
