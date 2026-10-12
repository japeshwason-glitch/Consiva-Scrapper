"""
Auto Email Campaign, milestone 2, stages 2 and 3: free email checks and domain resolution.

    run.sh api python -m unittest tests.test_campaign_checks -v

Nothing here touches the network: MX lookups go through an injected resolver. The database
tests use DbCase (a throwaway Postgres database per class) from tests.test_campaign.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db
from tests.test_campaign import DbCase

from campaign import checks, domains, settings_checks


class FakeResolver:
    """resolver(domain, timeout) -> list of MX hosts, or raises like the real one. Records every call."""

    def __init__(self, answers=None, default=("mx.example.net",)):
        self.answers, self.default, self.calls = answers or {}, default, []

    def __call__(self, domain, timeout):
        self.calls.append(domain)
        a = self.answers.get(domain, self.default)
        if isinstance(a, Exception):
            raise a
        return list(a)


# --- pure checks ---------------------------------------------------------------------------
class Syntax(unittest.TestCase):
    def test_good_addresses(self):
        for e in ("a@acme.in", "first.last@acme.co.in", "x+tag@sub.acme.com", "A@Acme.IN", "o'neil@acme.com"):
            self.assertTrue(checks.syntax_ok(e), e)

    def test_bad_addresses(self):
        for e in (None, "", "   ", "no-at-sign", "@acme.in", "a@", "a@acme", "a b@acme.in", "a@@acme.in",
                  "a@acme..in", ".a@acme.in", "a.@acme.in", "a@-acme.in", "a@acme.in.", "a@acme .in",
                  "a@acme.i", "x" * 65 + "@acme.in", "a@" + "x" * 250 + ".in", "a,b@acme.in", "a@acme.in;b@c.in"):
            self.assertFalse(checks.syntax_ok(e), repr(e))


class RoleAddresses(unittest.TestCase):
    def test_role_local_parts(self):
        for e in ("info@acme.in", "Contact@acme.in", "support@acme.in", "sales@acme.in", "hr@acme.in",
                  "admin@acme.in", "no-reply@acme.in", "careers@acme.in"):
            self.assertTrue(checks.is_role_address(e), e)

    def test_people_are_not_roles(self):
        for e in ("rahul@acme.in", "priya.sharma@acme.in", "ceo@acme.in", "information@acme.in"):
            self.assertFalse(checks.is_role_address(e), e)

    def test_the_list_is_config(self):
        with mock.patch.object(settings_checks, "ROLE_LOCAL_PARTS", frozenset({"boss"})):
            self.assertTrue(checks.is_role_address("boss@acme.in"))
            self.assertFalse(checks.is_role_address("info@acme.in"))


class FreeAndDisposable(unittest.TestCase):
    def test_free_mail(self):
        for e in ("a@gmail.com", "a@Yahoo.co.in", "a@outlook.com", "a@hotmail.com", "a@rediffmail.com"):
            self.assertTrue(checks.is_free_mail(e), e)
        self.assertFalse(checks.is_free_mail("a@acme.in"))

    def test_disposable_list_is_bundled_and_loaded_once(self):
        self.assertGreater(len(checks.disposable_domains()), 1000)
        self.assertIs(checks.disposable_domains(), checks.disposable_domains())
        self.assertTrue(checks.is_disposable("x@0-mail.com"))
        self.assertTrue(checks.is_disposable("x@0-MAIL.com"))
        self.assertFalse(checks.is_disposable("x@acme.in"))

    def test_the_bundled_file_records_its_source(self):
        head = settings_checks.DISPOSABLE_FILE.read_text().splitlines()[:3]
        self.assertTrue(any("github.com/disposable-email-domains" in l for l in head))
        self.assertTrue(any("Commit:" in l and "Fetched:" in l for l in head))


class DnsResolverMapping(unittest.TestCase):
    """The real resolver maps dnspython's outcomes onto: hosts / NoMx / MxUnavailable."""

    def run_with(self, side_effect):
        import dns.resolver
        with mock.patch.object(dns.resolver, "resolve", side_effect=side_effect) as m:
            try:
                return checks.dns_resolver("acme.in", 5), m
            except (checks.NoMx, checks.MxUnavailable) as e:
                return e, m

    def test_timeout_and_servfail_are_unavailable_not_failures(self):
        import dns.exception, dns.resolver
        for exc in (dns.exception.Timeout(), dns.resolver.NoNameservers(), dns.resolver.LifetimeTimeout()):
            got, _ = self.run_with(exc)
            self.assertIsInstance(got, checks.MxUnavailable, type(exc).__name__)

    def test_nxdomain_is_no_mx(self):
        import dns.resolver
        got, _ = self.run_with(dns.resolver.NXDOMAIN())
        self.assertIsInstance(got, checks.NoMx)

    def test_the_timeout_is_passed_to_dnspython_as_the_lifetime(self):
        import dns.resolver
        _, m = self.run_with(dns.resolver.NXDOMAIN())
        self.assertEqual(m.call_args.kwargs.get("lifetime"), 5)


# --- stage 2 against the database -------------------------------------------------------------
class ChecksCase(DbCase):
    def add_contact(self, company_id, email, cid=None):
        cid = cid or f"c-{company_id}-{email}"
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_contacts (contact_id, company_id, email, job_title) VALUES (%s,%s,%s,'CEO')",
                        (cid, company_id, email))
        return cid

    def contact(self, cid):
        with db.get_cursor() as cur:
            cur.execute("SELECT check_passed, check_reason, checked_at FROM ae_contacts WHERE contact_id = %s", (cid,))
            return cur.fetchone()

    def suppress(self, email):
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_suppression (email, reason, source) VALUES (%s,'bounced','test')", (email.lower(),))


class Stage2Contacts(ChecksCase):
    def run_stage(self, resolver=None, **kw):
        return checks.run(resolver=resolver or FakeResolver(), **kw)

    def check_one(self, email, resolver=None):
        self.add_company(1)
        cid = self.add_contact(1, email)
        self.run_stage(resolver)
        return self.contact(cid)

    def test_a_good_business_address_passes(self):
        row = self.check_one("rahul@acme.in")
        self.assertTrue(row["check_passed"])
        self.assertIsNotNone(row["checked_at"])

    def test_each_failure_names_its_reason(self):
        cases = {"not-an-email": "syntax", "": "syntax", "rahul@gmail.com": "free_mail",
                 "info@acme.in": "role_address", "rahul@0-mail.com": "disposable"}
        for i, (email, reason) in enumerate(cases.items(), start=1):
            self.add_company(i)
            cid = self.add_contact(i, email, cid=f"k{i}")
            self.run_stage()
            row = self.contact(cid)
            self.assertFalse(row["check_passed"], email)
            self.assertEqual(row["check_reason"], reason, email)

    def test_a_suppressed_address_fails_case_insensitively(self):
        self.suppress("Rahul@Acme.in")
        row = self.check_one("rahul@acme.IN")
        self.assertFalse(row["check_passed"])
        self.assertEqual(row["check_reason"], "suppressed")

    def test_a_null_email_fails_as_syntax(self):
        self.add_company(1)
        with db.get_cursor() as cur:
            cur.execute("INSERT INTO ae_contacts (contact_id, company_id, email) VALUES ('n1', 1, NULL)")
        self.run_stage()
        self.assertEqual(self.contact("n1")["check_reason"], "syntax")

    def test_first_failure_wins_the_reason(self):
        self.suppress("info@gmail.com")
        row = self.check_one("info@gmail.com")           # free mail, role and suppressed at once
        self.assertEqual(row["check_reason"], "free_mail")

    def test_no_mx_fails(self):
        row = self.check_one("rahul@dead.example", FakeResolver({"dead.example": checks.NoMx("NXDOMAIN")}))
        self.assertFalse(row["check_passed"])
        self.assertEqual(row["check_reason"], "no_mx")

    def test_a_mx_timeout_is_not_a_failure_and_is_flagged(self):
        r = FakeResolver({"slow.example": checks.MxUnavailable("timeout")})
        row = self.check_one("rahul@slow.example", r)
        self.assertTrue(row["check_passed"])
        self.assertEqual(row["check_reason"], "mx_unverified")

    def test_a_mx_timeout_is_recorded_but_never_served_from_cache(self):
        r = FakeResolver({"slow.example": checks.MxUnavailable("timeout")})
        self.add_company(1)
        self.add_contact(1, "a@slow.example")
        self.add_company(2)
        self.add_contact(2, "b@slow.example")
        self.run_stage(r)
        self.assertEqual(r.calls.count("slow.example"), 2, "an unavailable lookup must be asked again")
        with db.get_cursor() as cur:
            cur.execute("SELECT has_mx FROM ae_mx_cache WHERE domain = 'slow.example'")
            self.assertIsNone(cur.fetchone()["has_mx"])

    def test_mx_is_cached_per_domain(self):
        r = FakeResolver()
        for i in (1, 2, 3):
            self.add_company(i)
            self.add_contact(i, f"p{i}@acme.in")
        self.run_stage(r)
        self.assertEqual(r.calls, ["acme.in"])
        with db.get_cursor() as cur:
            cur.execute("SELECT has_mx FROM ae_mx_cache WHERE domain = 'acme.in'")
            self.assertTrue(cur.fetchone()["has_mx"])

    def test_a_cached_answer_is_used_across_runs_until_it_expires(self):
        r = FakeResolver()
        self.add_company(1)
        self.add_contact(1, "a@acme.in")
        self.run_stage(r)
        self.add_company(2)
        self.add_contact(2, "b@acme.in")
        self.run_stage(r)
        self.assertEqual(r.calls, ["acme.in"])
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_mx_cache SET checked_at = NOW() - INTERVAL '30 days'")
        self.add_company(3)
        self.add_contact(3, "c@acme.in")
        self.run_stage(r)
        self.assertEqual(r.calls, ["acme.in", "acme.in"])

    def test_a_cached_no_mx_fails_without_a_new_lookup(self):
        r = FakeResolver({"dead.example": checks.NoMx("x")})
        for i in (1, 2):
            self.add_company(i)
            self.add_contact(i, f"p{i}@dead.example")
        self.run_stage(r)
        self.assertEqual(r.calls, ["dead.example"])
        self.assertEqual(self.contact("c-2-p2@dead.example")["check_reason"], "no_mx")

    def test_the_mx_lookup_is_skipped_for_addresses_that_already_failed(self):
        r = FakeResolver()
        self.add_company(1)
        self.add_contact(1, "info@acme.in")
        self.add_contact(1, "bob@gmail.com")
        self.run_stage(r)
        self.assertEqual(r.calls, [])


class Stage2Companies(ChecksCase):
    def test_one_passing_contact_is_enough(self):
        self.add_company(1)
        self.add_contact(1, "info@acme.in")
        self.add_contact(1, "rahul@acme.in")
        counts = checks.run(resolver=FakeResolver())
        status, reason = self.status_of(1)
        self.assertEqual(status, "checked")
        self.assertIn("1", reason)
        self.assertEqual(counts["checked"], 1)

    def test_no_passing_contact_means_skipped_invalid_with_the_reasons(self):
        self.add_company(1)
        self.add_contact(1, "info@acme.in")
        self.add_contact(1, "sales@acme.in")
        self.add_contact(1, "bob@gmail.com")
        counts = checks.run(resolver=FakeResolver())
        status, reason = self.status_of(1)
        self.assertEqual(status, "skipped_invalid")
        self.assertIn("2 role_address", reason)
        self.assertIn("1 free_mail", reason)
        self.assertEqual(counts["skipped_invalid"], 1)
        self.assertEqual(counts["fail_reasons"], {"role_address": 2, "free_mail": 1})

    def test_a_company_with_no_contacts_is_skipped_invalid(self):
        self.add_company(1)
        checks.run(resolver=FakeResolver())
        self.assertEqual(self.status_of(1), ("skipped_invalid", "no contacts"))

    def test_only_pending_companies_are_touched(self):
        self.add_company(1, "checked")
        self.add_company(2, "legacy")
        self.add_company(3, "reported")
        self.add_contact(1, "info@acme.in")
        checks.run(resolver=FakeResolver())
        self.assertEqual([self.status_of(i)[0] for i in (1, 2, 3)], ["checked", "legacy", "reported"])
        self.assertIsNone(self.contact("c-1-info@acme.in")["check_passed"])

    def test_limit_bounds_the_work(self):
        for i in range(1, 6):
            self.add_company(i)
            self.add_contact(i, f"p{i}@acme.in")
        counts = checks.run(limit=2, resolver=FakeResolver())
        self.assertEqual(counts["claimed"], 2)
        self.assertEqual(self.count("ae_companies", "status = 'checked'"), 2)

    def test_only_test_restricts_the_cohort(self):
        self.add_company(1, is_test=True)
        self.add_company(2, is_test=False)
        for i in (1, 2):
            self.add_contact(i, f"p{i}@acme.in")
        checks.run(only_test=True, resolver=FakeResolver())
        self.assertEqual(self.status_of(1)[0], "checked")
        self.assertEqual(self.status_of(2)[0], "pending")
        checks.run(only_test=False, resolver=FakeResolver())
        self.assertEqual(self.status_of(2)[0], "checked")

    def test_a_rerun_changes_nothing(self):
        self.add_company(1)
        self.add_contact(1, "rahul@acme.in")
        checks.run(resolver=FakeResolver())
        r = FakeResolver()
        again = checks.run(resolver=r)
        self.assertEqual(again["claimed"], 0)
        self.assertEqual(r.calls, [])
        self.assertEqual(self.status_of(1)[0], "checked")

    def test_dry_run_writes_nothing_but_says_what_it_would_do(self):
        self.add_company(1)
        self.add_company(2)
        self.add_contact(1, "rahul@acme.in")
        self.add_contact(2, "info@acme.in")
        counts = checks.run(dry_run=True, resolver=FakeResolver())
        self.assertEqual(counts["checked"], 1)
        self.assertEqual(counts["skipped_invalid"], 1)
        self.assertEqual([self.status_of(i)[0] for i in (1, 2)], ["pending", "pending"])
        self.assertEqual(self.count("ae_contacts", "check_passed IS NOT NULL"), 0)
        self.assertEqual(self.count("ae_mx_cache"), 0)

    def test_no_crm_write_for_these_statuses(self):
        from campaign import crm_write
        self.add_company(1)
        self.add_contact(1, "rahul@acme.in")
        with mock.patch.object(crm_write, "update_company", side_effect=AssertionError("CRM write")):
            checks.run(resolver=FakeResolver())
        self.assertEqual(self.status_of(1)[0], "checked")


# --- stage 3 ------------------------------------------------------------------------------------
class Stage3Domain(ChecksCase):
    def checked(self, company_id, domain="acme.in", is_test=False):
        self.add_company(company_id, "checked", is_test=is_test, domain=domain)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_companies SET domain = %s WHERE company_id = %s", (domain, company_id))

    def passed_contact(self, company_id, email):
        cid = self.add_contact(company_id, email)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_contacts SET check_passed = TRUE WHERE contact_id = %s", (cid,))

    def failed_contact(self, company_id, email):
        cid = self.add_contact(company_id, email)
        with db.get_cursor() as cur:
            cur.execute("UPDATE ae_contacts SET check_passed = FALSE, check_reason = 'role_address' "
                        "WHERE contact_id = %s", (cid,))

    def row(self, company_id):
        with db.get_cursor() as cur:
            cur.execute("SELECT domain, domain_source, status FROM ae_companies WHERE company_id = %s", (company_id,))
            return dict(cur.fetchone())

    def test_a_website_gives_the_domain_with_source_website(self):
        self.checked(1, "acme.in")
        counts = domains.run()
        self.assertEqual(self.row(1), {"domain": "acme.in", "domain_source": "website", "status": "checked"})
        self.assertEqual(counts["website"], 1)

    def test_no_website_and_one_shared_email_domain(self):
        self.checked(1, None)
        self.passed_contact(1, "a@acme.in")
        self.passed_contact(1, "b@acme.in")
        domains.run()
        self.assertEqual(self.row(1), {"domain": "acme.in", "domain_source": "email", "status": "checked"})

    def test_no_website_and_a_clear_majority(self):
        self.checked(1, None)
        for e in ("a@acme.in", "b@acme.in", "c@acme.in", "d@other.in"):
            self.passed_contact(1, e)
        domains.run()
        self.assertEqual(self.row(1)["domain"], "acme.in")        # 3 of 4 = 75%
        self.assertEqual(self.row(1)["domain_source"], "email")

    def test_no_website_and_mixed_domains_is_no_website(self):
        self.checked(1, None)
        self.passed_contact(1, "a@acme.in")
        self.passed_contact(1, "b@other.in")
        counts = domains.run()
        self.assertEqual(self.status_of(1)[0], "no_website")
        self.assertEqual(counts["no_website"], 1)
        self.assertIsNone(self.row(1)["domain"])

    def test_exactly_the_majority_share_counts(self):
        self.assertEqual(settings_checks.DOMAIN_MAJORITY_SHARE, 0.6)
        self.checked(1, None)
        for e in ("a@acme.in", "b@acme.in", "c@acme.in", "d@x.in", "e@y.in"):
            self.passed_contact(1, e)
        domains.run()                                               # 3 of 5 = 60%
        self.assertEqual(self.row(1)["domain"], "acme.in")

    def test_just_under_the_share_is_mixed(self):
        self.checked(1, None)
        for e in ("a@acme.in", "b@acme.in", "c@x.in", "d@y.in", "e@z.in"):
            self.passed_contact(1, e)
        domains.run()                                               # 2 of 5 = 40%
        self.assertEqual(self.status_of(1)[0], "no_website")

    def test_only_passing_business_contacts_vote(self):
        self.checked(1, None)
        self.passed_contact(1, "a@acme.in")
        self.failed_contact(1, "info@other.in")
        self.failed_contact(1, "info@another.in")
        self.passed_contact(1, "x@gmail.com")                       # free mail never votes
        domains.run()
        self.assertEqual(self.row(1)["domain"], "acme.in")

    def test_no_website_and_no_passing_business_contact_is_no_website(self):
        self.checked(1, None)
        self.failed_contact(1, "info@acme.in")
        domains.run()
        self.assertEqual(self.status_of(1)[0], "no_website")

    def test_the_crm_is_never_touched(self):
        from campaign import crm_write
        self.checked(1, None)
        self.passed_contact(1, "a@acme.in")
        with mock.patch.object(crm_write, "update_company", side_effect=AssertionError("CRM write")), \
                mock.patch.object(crm_write, "_connect", side_effect=AssertionError("CRM connection")):
            domains.run()
        self.assertEqual(self.row(1)["domain"], "acme.in")

    # duplicates
    def test_the_lowest_company_id_proceeds_and_the_other_is_held(self):
        self.checked(2, "acme.in")
        self.checked(1, "acme.in")
        domains.run()
        self.assertEqual(self.status_of(1)[0], "checked")
        self.assertEqual(self.status_of(2), ("held_duplicate_domain", "duplicate of company 1"))

    def test_the_company_that_already_holds_the_domain_wins_even_with_a_higher_id(self):
        self.checked(5, "acme.in")
        domains.run()
        self.checked(3, "acme.in")                                  # lower id, but 5 already took it
        domains.run()
        self.assertEqual(self.status_of(5)[0], "checked")
        self.assertEqual(self.status_of(3), ("held_duplicate_domain", "duplicate of company 5"))

    def test_a_legacy_company_holds_its_domain(self):
        self.add_company(10, "legacy", domain="acme.in")
        self.checked(20, "acme.in")
        domains.run()
        self.assertEqual(self.status_of(20), ("held_duplicate_domain", "duplicate of company 10"))

    def test_an_email_derived_domain_is_checked_for_duplicates_too(self):
        self.checked(1, "acme.in")
        self.checked(2, None)
        self.passed_contact(2, "a@acme.in")
        domains.run()
        self.assertEqual(self.status_of(2), ("held_duplicate_domain", "duplicate of company 1"))

    def test_a_dead_lower_id_company_does_not_hold_the_domain(self):
        self.add_company(1, "skipped_invalid", domain="acme.in")
        self.checked(2, "acme.in")
        domains.run()
        self.assertEqual(self.status_of(2)[0], "checked")

    def test_a_held_company_does_not_hold_the_domain(self):
        self.add_company(1, "held_duplicate_domain", domain="acme.in")
        self.checked(2, "acme.in")
        domains.run()
        self.assertEqual(self.status_of(2)[0], "checked")

    def test_domains_are_compared_case_insensitively(self):
        self.checked(1, "acme.in")
        self.checked(2, "ACME.in")
        domains.run()
        self.assertEqual(self.status_of(2)[0], "held_duplicate_domain")

    def test_test_companies_skip_the_duplicate_rule(self):
        self.add_company(1, "legacy", domain="acme.in")
        self.checked(2, "acme.in", is_test=True)
        domains.run()
        self.assertEqual(self.status_of(2)[0], "checked")
        self.assertEqual(self.row(2)["domain_source"], "website")

    def test_a_test_company_never_holds_a_domain_against_production(self):
        self.checked(1, "acme.in", is_test=True)
        self.checked(2, "acme.in")
        domains.run()
        self.assertEqual(self.status_of(2)[0], "checked")

    # run mechanics
    def test_only_unresolved_checked_companies_are_claimed(self):
        self.checked(1, "acme.in")
        self.add_company(2, "pending", domain="b.in")
        self.add_company(3, "reported", domain="c.in")
        counts = domains.run()
        self.assertEqual(counts["claimed"], 1)
        self.assertIsNone(self.row(2)["domain_source"])
        self.assertIsNone(self.row(3)["domain_source"])

    def test_a_rerun_changes_nothing(self):
        self.checked(1, "acme.in")
        domains.run()
        again = domains.run()
        self.assertEqual(again["claimed"], 0)
        self.assertEqual(self.row(1)["domain_source"], "website")

    def test_limit_and_only_test(self):
        for i in range(1, 5):
            self.checked(i, f"d{i}.in")
        self.checked(9, "t.in", is_test=True)
        self.assertEqual(domains.run(limit=2)["claimed"], 2)
        domains.run(only_test=True)
        self.assertEqual(self.row(9)["domain_source"], "website")
        self.assertEqual(domains.run(only_test=False)["claimed"], 2)

    def test_dry_run_writes_nothing_but_counts(self):
        self.checked(1, "acme.in")
        self.checked(2, "acme.in")
        self.checked(3, None)
        self.passed_contact(3, "a@z.in")
        counts = domains.run(dry_run=True)
        self.assertEqual((counts["website"], counts["held_duplicate_domain"], counts["email"]), (1, 1, 1))
        self.assertEqual([self.row(i)["domain_source"] for i in (1, 2, 3)], [None, None, None])
        self.assertEqual([self.status_of(i)[0] for i in (1, 2, 3)], ["checked"] * 3)
        self.assertIsNone(self.row(3)["domain"])


if __name__ == "__main__":
    unittest.main()
