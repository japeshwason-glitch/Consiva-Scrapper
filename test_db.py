"""
Live Postgres test for db.py. Not a unit test -- it runs against the real
database in DATABASE_URL and asserts on what is actually stored.

    python3 test_db.py

Uses CINs under a TEST- prefix and deletes them on the way in and out, so it
is safe to run against the working database repeatedly.
"""

import sys

import db

TEST_CINS = ["TEST-NOTALEAD-0001", "TEST-RETRY-0002", "TEST-JSONB-0003"]

passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{f' -- {detail}' if detail else ''}")


def cleanup():
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM companies WHERE cin = ANY(%s)", (TEST_CINS,))


def insert(cin, **extra):
    cols = {"cin": cin, "company_name": f"Fixture {cin}", **extra}
    placeholders = ", ".join(["%s"] * len(cols))
    with db.get_cursor() as cur:
        cur.execute(
            f"INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders})",
            list(cols.values()),
        )


def fetch(cin):
    with db.get_cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE cin = %s", (cin,))
        return cur.fetchone()


def test_mark_not_a_lead_records_reason():
    """The specific case that was silently broken under SQLite."""
    print("\n[1] mark_not_a_lead writes the reason into last_error")
    cin = TEST_CINS[0]
    insert(cin, status="scraped", has_cmp=True)
    db.mark_not_a_lead(cin, "already has a CMP: OneTrust")

    row = fetch(cin)
    check("status is 'not_a_lead'", row["status"] == "not_a_lead", row["status"])
    check(
        "last_error holds the reason string",
        row["last_error"] == "already has a CMP: OneTrust",
        repr(row["last_error"]),
    )
    check("retry_count reset to 0", row["retry_count"] == 0, row["retry_count"])

    # The default-argument path must record a reason too.
    db.mark_not_a_lead(cin)
    check(
        "default reason also persists",
        fetch(cin)["last_error"] == "already has a CMP",
        repr(fetch(cin)["last_error"]),
    )


def test_retry_path():
    print(f"\n[2] mark_failed retries in place, flips to *_failed at MAX_RETRIES={db.MAX_RETRIES}")
    cin = TEST_CINS[1]
    insert(cin, status="resolved")

    for attempt in range(1, db.MAX_RETRIES):
        db.mark_failed(cin, "scrape", f"timeout on attempt {attempt}")
        row = fetch(cin)
        check(
            f"attempt {attempt}: status stays 'resolved', retry_count={attempt}",
            row["status"] == "resolved" and row["retry_count"] == attempt,
            f"status={row['status']} retry_count={row['retry_count']}",
        )
        check(
            f"attempt {attempt}: row still visible to get_pending('resolved')",
            any(r["cin"] == cin for r in db.get_pending("resolved", limit=500)),
        )

    db.mark_failed(cin, "scrape", "timeout on final attempt")
    row = fetch(cin)
    check(
        f"attempt {db.MAX_RETRIES}: status flips to 'scrape_failed'",
        row["status"] == "scrape_failed",
        row["status"],
    )
    check("retry_count at threshold", row["retry_count"] == db.MAX_RETRIES, row["retry_count"])
    check("last_error retained", row["last_error"] == "timeout on final attempt")
    check(
        "row no longer returned by get_pending('resolved')",
        not any(r["cin"] == cin for r in db.get_pending("resolved", limit=500)),
    )

    # A later success clears the retry state.
    db.advance_status(cin, "scraped", has_cmp=False)
    row = fetch(cin)
    check("advance_status resets retry_count", row["retry_count"] == 0, row["retry_count"])
    check("advance_status clears last_error", row["last_error"] is None, repr(row["last_error"]))


def test_jsonb_roundtrip():
    print("\n[3] JSONB round-trip returns Python objects, not JSON strings")
    cin = TEST_CINS[2]
    insert(cin, status="resolved")

    roles = ["Backend Engineer", "DPO", "Growth Lead"]
    cookies = ["_ga", "_fbp", "_gcl_au"]
    clients = [{"name": "Acme", "logo": "/acme.png"}, {"name": "Globex"}]

    db.advance_status(
        cin, "scraped",
        has_cmp=False,
        notable_open_roles=roles,
        pre_consent_cookies=cookies,
        notable_clients=clients,
        tech_analytics_tools=["GA4"],
        tech_marketing_tools=[],
    )

    row = fetch(cin)
    check("notable_open_roles is a list", isinstance(row["notable_open_roles"], list),
          type(row["notable_open_roles"]).__name__)
    check("notable_open_roles round-trips intact", row["notable_open_roles"] == roles)
    check("pre_consent_cookies round-trips intact", row["pre_consent_cookies"] == cookies)
    check("notable_clients is a list of dicts",
          isinstance(row["notable_clients"], list)
          and all(isinstance(c, dict) for c in row["notable_clients"]))
    check("notable_clients round-trips intact", row["notable_clients"] == clients)
    check("empty list stays an empty list", row["tech_marketing_tools"] == [])

    # Same guarantee through the read path the stages actually use.
    pending = [r for r in db.get_pending("scraped", limit=500) if r["cin"] == cin]
    check("get_pending() also yields a list", pending and isinstance(pending[0]["notable_open_roles"], list))


def test_has_cmp_boolean():
    print("\n[4] has_cmp is a native BOOLEAN and filters with true/false")
    row = fetch(TEST_CINS[2])
    check("has_cmp is a Python bool", isinstance(row["has_cmp"], bool), type(row["has_cmp"]).__name__)

    cmp_free = db.get_pending("scraped", limit=500, extra_where="has_cmp = false")
    check(
        "extra_where='has_cmp = false' returns the CMP-free row",
        any(r["cin"] == TEST_CINS[2] for r in cmp_free),
    )
    check(
        "stage-04 filter excludes every has_cmp=true row",
        all(r["has_cmp"] is False for r in cmp_free),
    )


def test_timestamps():
    print("\n[5] timestamps are real TIMESTAMPTZ values")
    from datetime import datetime
    row = fetch(TEST_CINS[2])
    check("created_at is a datetime", isinstance(row["created_at"], datetime),
          type(row["created_at"]).__name__)
    check("updated_at is tz-aware", row["updated_at"].tzinfo is not None)
    check("updated_at advanced past created_at", row["updated_at"] >= row["created_at"])


def main():
    if not __import__("config").DATABASE_URL:
        sys.exit("DATABASE_URL is not set -- see .env.example")

    db.init_db()
    cleanup()
    try:
        test_mark_not_a_lead_records_reason()
        test_retry_path()
        test_jsonb_roundtrip()
        test_has_cmp_boolean()
        test_timestamps()
    finally:
        cleanup()
        db.close_pool()

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
