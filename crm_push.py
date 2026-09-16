"""
Pushes leads into the sales team's SQL Server table (dbo.CRM_Scrapper).

    python crm_push.py --test          # insert ONE row, then show it back
    python crm_push.py --limit 50      # push the next 50 undelivered leads
    python crm_push.py --dry-run       # print the SQL and values, send nothing

Their database, not ours. Id is an identity column and CreatedAt/UpdatedAt are
left to the server, so we only supply the business fields.

Rows we push are marked delivered_at locally so the same lead is never sent
twice -- the CRM table has no CIN column, so duplicate protection has to live
on our side.
"""

import argparse
import sys

import pymssql

import config
import db
import resolver

# Their column -> how we fill it. CookieBanner and CollectsViaForm are
# nvarchar, not flags, so they carry a readable phrase rather than 0/1.
COLUMNS = ("CompanyName", "Website", "GapScore", "ContactName", "ContactTitle",
           "CompanyScale", "Industry", "TrackersPreConsent", "ThirdPartiesCount",
           "CookieBanner", "CollectsViaForm", "ConsentFinding")


def clip(value, n):
    if value is None:
        return None
    return str(value)[:n]


# Their scale runs the other way from ours. Internally a HIGH score means a
# BIG gap, which is how every pipeline filter reads it. On their side 100 means
# clean and 0 means worst, so the value is flipped once, here, at the boundary.
#
# Consequences on their side, which do not apply to ours:
#   a lead  is GapScore < 100   (was > 0)
#   severe  is GapScore <= 50   (was >= 50)
OUTWARD_SCORE_MAX = 100


def outward_gap_score(internal):
    """
    Our 0-100 (high = worse) as their 0-100 (high = better).

    Returns None for None so an unmeasured row stays unmeasured rather than
    becoming a perfect 100.
    """
    if internal is None:
        return None
    return OUTWARD_SCORE_MAX - int(internal)


def form_sentence(c):
    """
    The CollectsViaForm phrase, or "" when the site takes no personal data.

    Shared with the inbound job so both produce the same wording for the same
    finding -- they write to different tables and would otherwise drift.
    """
    if not c.get("has_data_form"):
        return ""
    fields = ", ".join(c.get("form_field_types") or [])
    form = f"{fields} — no consent checkbox" if not c.get("form_consent_checkbox") \
           else f"{fields} — consent checkbox present"
    hosts = c.get("form_third_party_hosts") or []
    if hosts:
        form += f"; submitted to {', '.join(hosts)}"
    return form


def to_row(c):
    form = form_sentence(c)

    return (
        clip(c.get("company_name"), 250),
        clip(c.get("final_domain") or c.get("resolved_domain"), 500),
        outward_gap_score(c.get("consent_gap_score")),
        clip(c.get("contact_name"), 200),
        clip(c.get("contact_title"), 200),
        clip(c.get("business_scale"), 100),
        clip(c.get("linkedin_industry") or c.get("industrial_classification"), 200),
        c.get("tracking_cookie_count"),
        c.get("third_party_count"),
        clip("present" if c.get("has_cookie_banner") else "none", 100),
        form or None,
        c.get("consent_gap_summary"),
    )


def connect():
    missing = [k for k in ("CRM_DB_HOST", "CRM_DB_NAME", "CRM_DB_USER", "CRM_DB_PASSWORD")
               if not getattr(config, k)]
    if missing:
        sys.exit(f"missing in .env: {', '.join(missing)}")
    return pymssql.connect(
        server=config.CRM_DB_HOST, port=str(config.CRM_DB_PORT),
        user=config.CRM_DB_USER, password=config.CRM_DB_PASSWORD,
        database=config.CRM_DB_NAME, timeout=30, login_timeout=20)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--test", action="store_true",
                    help="insert one row and read it back, to prove the connection")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="also send leads resolved before the identity check "
                         "existed (resolve_match_score IS NULL). Off by "
                         "default: those are the rows that matched the wrong "
                         "company entirely.")
    args = ap.parse_args()

    limit = 1 if args.test else args.limit
    # A lead is a company with a demonstrable consent gap. ContactName may be
    # NULL -- Apollo covers barely 10% of small Indian firms, and sales sources
    # contacts via Lusha anyway. What must never reach their table is a company
    # with nothing to sell against, so the gap and the absence of a CMP are
    # enforced in the query itself.
    # The domain must also have been confirmed to belong to this company.
    # Wrong domains score HIGHEST on the gap -- a big tracker-heavy site
    # unrelated to the company outscores the company's own small site -- so
    # without this they sort to the front of the queue and go out first.
    verified = "" if args.allow_unverified else \
               f" AND resolve_match_score >= {resolver.ACCEPT_SCORE}"
    leads = [c for c in db.get_pending(
                 "exported", limit=limit * 5,
                 extra_where="delivered_at IS NULL AND has_cmp = false "
                             "AND consent_gap_score > 0" + verified)][:limit]
    if not leads:
        sys.exit("nothing at status='exported' waiting to be delivered")

    rows = [to_row(c) for c in leads]
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    sql = f"INSERT INTO {config.CRM_DB_TABLE} ({', '.join(COLUMNS)}) VALUES ({placeholders})"

    if args.dry_run:
        print(sql)
        for r in rows:
            print("  " + " | ".join(f"{v!r}"[:46] for v in r))
        print(f"\ndry run -- {len(rows)} rows, nothing sent")
        return

    # Claim the leads BEFORE inserting. If the machine dies mid-push the worst
    # case is a lead marked delivered that never arrived -- recoverable, and
    # visible. The other ordering risks a duplicate row in THEIR table, which
    # has no CIN column and therefore no way to detect or clean up.
    cins = [c["cin"] for c in leads]
    with db.get_cursor() as local:
        local.execute("UPDATE companies SET delivered_at = NOW() WHERE cin = ANY(%s)",
                      (cins,))

    conn = connect()
    try:
        cur = conn.cursor()
        cur.executemany(sql, rows)
        conn.commit()
    except Exception:
        # Release the claim so the next run retries them.
        with db.get_cursor() as local:
            local.execute("UPDATE companies SET delivered_at = NULL WHERE cin = ANY(%s)",
                          (cins,))
        raise
    print(f"inserted {len(rows)} row(s) into {config.CRM_DB_TABLE}")

    if args.test:
        cur.execute(f"SELECT TOP 1 Id, CompanyName, Website, GapScore, CreatedAt "
                    f"FROM {config.CRM_DB_TABLE} ORDER BY Id DESC")
        print("\nread back from their server:")
        for name, value in zip(("Id", "CompanyName", "Website", "GapScore", "CreatedAt"),
                               cur.fetchone()):
            print(f"  {name:<12} {value}")
    conn.close()

    print(f"\n{len(leads)} lead(s) delivered")


if __name__ == "__main__":
    main()
