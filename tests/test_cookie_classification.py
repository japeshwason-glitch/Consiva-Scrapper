"""
Cookie classification (fingerprints.FINGERPRINTS_VERSION = 2026-10-cookie-fix.2).

    python -m unittest tests.test_cookie_classification -v

stdlib unittest so it runs in the pipeline image, which does not ship pytest.
"""
import sys
import time
import unittest
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stages"))

import fingerprints as fp

DAY = 86400


def raw(name, domain, days=None):
    """A cookie as Playwright's context.cookies() returns it."""
    return {"name": name, "domain": domain,
            "expires": -1 if days is None else time.time() + days * DAY + 60,
            "secure": False, "httpOnly": False, "sameSite": "Lax"}


def category(name, domain, site="example.in"):
    return fp.cookie_record(raw(name, domain), site)["category"]


def reclassify(records):
    """What the offline recompute does to stored records."""
    return [{**r, "category": fp.cookie_category(r["name"], r["domain"], r["party"] == "third")}
            for r in records]


class Necessary(unittest.TestCase):
    def test_grecaptcha_on_google_is_necessary(self):
        self.assertEqual(category("_GRECAPTCHA", "www.google.com"), "necessary")

    def test_cf_bm_is_necessary_on_any_domain(self):
        for domain in ("example.in", ".teepublic.com", "cdn.cloudflare.net"):
            self.assertEqual(category("__cf_bm", domain), "necessary", domain)

    def test_prefix_entries(self):
        self.assertEqual(category("cf_chl_rc_m", "example.in"), "necessary")
        self.assertEqual(category("ASPSESSIONIDQQRTSBAD", "example.in"), "necessary")
        self.assertEqual(category("wp-settings-time-1", "example.in"), "necessary")
        self.assertEqual(category("cmplz_marketing", "example.in"), "necessary")

    def test_necessary_never_counts(self):
        a = fp.analyse_cookies([raw("_GRECAPTCHA", "www.google.com", 179),
                                raw("cf_clearance", ".example.in", 364)], "example.in")
        self.assertEqual(a["tracking_count"], 0)
        self.assertEqual(a["third_party_domains"], [])
        self.assertIsNone(a["max_lifetime_days"])


class ExactMatch(unittest.TestCase):
    def test_frontend_session_is_not_facebook(self):
        self.assertEqual(category("frontend_session", "example.in"), "functional")

    def test_fr_on_facebook_is_advertising(self):
        self.assertEqual(category("fr", ".facebook.com"), "advertising")

    def test_fr_elsewhere_is_not_advertising(self):
        self.assertEqual(category("fr", "example.in"), "functional")

    def test_nid_on_own_domain_is_not_advertising(self):
        self.assertNotEqual(category("NID", "example.in"), "advertising")
        self.assertEqual(category("NID", ".google.com"), "advertising")

    def test_muid_needs_microsoft_domain(self):
        self.assertEqual(category("MUID", ".bing.com"), "session_replay")
        self.assertEqual(category("MUID", ".clarity.ms"), "session_replay")
        self.assertEqual(category("MUID", "example.in"), "functional")


class Unclassified(unittest.TestCase):
    def test_unknown_third_party_cookie_is_unclassified_and_uncounted(self):
        a = fp.analyse_cookies([raw("SRM_B", ".c.bing-like.net", 390)], "example.in")
        self.assertEqual(a["records"][0]["category"], "unclassified")
        self.assertEqual(a["cookies_total"], 1)           # still recorded
        self.assertEqual(a["tracking_count"], 0)
        self.assertEqual(a["tracking"], [])
        self.assertEqual(a["third_party_count"], 0)
        self.assertEqual(a["third_party_domains"], [])
        self.assertEqual(a["long_lived_count"], 0)
        self.assertIsNone(a["max_lifetime_days"])
        self.assertEqual(fp.gap_score(False, a, False, False), 0)
        self.assertEqual(fp.consent_score(False, a, False, False, None), fp.ALIGNED)

    def test_unclassified_not_in_summary_tracking_list(self):
        s3 = import_module("03_scrape_playwright")
        a = fp.analyse_cookies([raw("SRM_B", ".c.bing-like.net", 390),
                                raw("_ga", ".example.in", 399)], "example.in")
        text = s3.build_consent_summary(None, a, False, True)
        self.assertIn("1 of 2 cookies are tracking cookies set before consent: _ga;", text)
        self.assertNotIn("SRM_B", text)
        self.assertNotIn("third-party", text)


class UnchangedTrackers(unittest.TestCase):
    def test_known_trackers_keep_their_categories(self):
        self.assertEqual(category("_ga", ".example.in"), "analytics")
        self.assertEqual(category("_ga_DNTS9ZFQ29", ".example.in"), "analytics")
        self.assertEqual(category("_fbp", ".example.in"), "advertising")
        self.assertEqual(category("_hjSessionUser_123", ".example.in"), "session_replay")


class VendorTrackers(unittest.TestCase):
    """Clarity/Bing, LinkedIn and YouTube cookies: tracking only on their own domain."""
    CASES = {
        "MR": (".c.clarity.ms", "session_replay"),
        "CLID": ("www.clarity.ms", "session_replay"),
        "SM": (".c.clarity.ms", "session_replay"),
        "ANONCHK": (".c.bing.com", "session_replay"),
        "SRM_B": (".c.bing.com", "advertising"),
        "bscookie": (".www.linkedin.com", "advertising"),
        "AnalyticsSyncHistory": (".linkedin.com", "advertising"),
        "VISITOR_PRIVACY_METADATA": (".youtube.com", "advertising"),
        "__Secure-YNID": (".youtube.com", "advertising"),
        "__Secure-ROLLOUT_TOKEN": (".youtube.com", "advertising"),
    }

    def test_on_vendor_domain_is_tracking(self):
        for name, (domain, cat) in self.CASES.items():
            self.assertEqual(category(name, domain), cat, name)

    def test_short_names_elsewhere_are_not_tracking(self):
        self.assertEqual(category("SM", "example.in"), "functional")
        self.assertEqual(category("MR", "example.in"), "functional")
        self.assertEqual(category("CLID", ".some-cdn.net"), "unclassified")

    def test_clarity_cluster_counts(self):
        a = fp.analyse_cookies([raw("MUID", ".clarity.ms", 390), raw("MR", ".c.clarity.ms", 7),
                                raw("SRM_B", ".c.bing.com", 390)], "example.in")
        self.assertEqual(a["tracking_count"], 3)
        self.assertEqual(a["third_party_domains"], ["c.bing.com", "c.clarity.ms", "clarity.ms"])


class AuditSamples(unittest.TestCase):
    # Stored pre_consent_cookies of the audit's severe sample, verbatim fields.
    SANATAN = [
        {"name": "_GRECAPTCHA", "party": "third", "domain": "www.google.com", "lifetime_days": 179, "category": "third_party"},
        {"name": "_gid", "party": "first", "domain": "sanatantech.com", "lifetime_days": 0, "category": "analytics"},
        {"name": "_gat_gtag_UA_76628358_1", "party": "first", "domain": "sanatantech.com", "lifetime_days": 0, "category": "analytics"},
        {"name": "_lfa", "party": "first", "domain": "sanatantech.com", "lifetime_days": 364, "category": "analytics"},
        {"name": "_ga_DNTS9ZFQ29", "party": "first", "domain": "sanatantech.com", "lifetime_days": 399, "category": "analytics"},
        {"name": "_ga", "party": "first", "domain": "sanatantech.com", "lifetime_days": 399, "category": "analytics"},
    ]

    def test_sanatan_tracking_6_to_5(self):
        a = fp.summarise_records(reclassify(self.SANATAN))
        self.assertEqual(a["tracking_count"], 5)
        self.assertNotIn("_GRECAPTCHA", [r["name"] for r in a["tracking"]])
        self.assertEqual(a["third_party_count"], 0)
        form = {"has_data_form": True, "consent_checkbox": False,
                "privacy_link": False, "external_hosts": []}
        score = fp.consent_score(False, a, False, False, form)
        print(f"\n  Sanatan Tech: tracking 6 -> {a['tracking_count']}, "
              f"third parties 1 -> {a['third_party_count']}, score 7 -> {score}")
        self.assertEqual(score, 11)
        self.assertTrue(fp.is_severe_score(score))

    # Rich Offset is a partner row, so no cookie records were stored. Rebuilt
    # from its finding: "4 of 5 cookies are tracking ...: __cf_bm, _ga, _gid,
    # NID; data sent to 2 third-party domains: google.com, teepublic.com".
    RICH_OFFSET = [
        raw("__cf_bm", ".teepublic.com", 0),
        raw("_ga", ".richoffset.com", 399),
        raw("_gid", ".richoffset.com", 1),
        raw("NID", ".google.com", 183),
        raw("wordpress_test_cookie", "richoffset.com"),
    ]

    def test_rich_offset_cf_bm_not_tracking(self):
        a = fp.analyse_cookies(self.RICH_OFFSET, "richoffset.com")
        names = [r["name"] for r in a["tracking"]]
        self.assertNotIn("__cf_bm", names)
        self.assertEqual(sorted(names), ["NID", "_ga", "_gid"])
        self.assertEqual(a["third_party_domains"], ["google.com"])
        self.assertEqual(fp.consent_score(True, a, False, True, None), fp.ALIGNED)
        print(f"\n  Rich Offset: tracking 4 -> {a['tracking_count']}, "
              f"third parties 2 -> {a['third_party_count']} ({', '.join(a['third_party_domains'])})")


class Version(unittest.TestCase):
    def test_version_constant(self):
        self.assertEqual(fp.FINGERPRINTS_VERSION, "2026-10-cookie-fix.2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
