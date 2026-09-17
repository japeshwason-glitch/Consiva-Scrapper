"""
Fills TargetTitle1..3 on delivered leads that have no contact attached.

    python backfill_target_titles.py --dry-run
    python backfill_target_titles.py

Roughly 8 in 10 leads reach the CRM without a name, because the contact
provider has no record of most small Indian companies. Sales sources those
through Lusha, and these three columns say WHICH titles to ask for.

Driven from our own business_scale, industry and CIN, so re-running is safe:
the same inputs always produce the same three titles. Rows that already carry
a contact are left blank on purpose.
"""
import argparse

import config
import crm_push
import db
import personas


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with db.get_cursor() as cur:
        cur.execute("""SELECT cin, company_name, business_scale,
                              COALESCE(linkedin_industry, industrial_classification) AS industry,
                              COALESCE(final_domain, resolved_domain) AS domain,
                              contact_name
                       FROM companies WHERE delivered_at IS NOT NULL""")
        rows = [dict(r) for r in cur.fetchall()]

    need = [r for r in rows if not (r["contact_name"] or "").strip()]
    print(f"{len(rows)} delivered, {len(need)} without a contact\n")

    conn = crm_push.connect()
    cur = conn.cursor()
    written = 0
    shown = {}
    for r in need:
        titles = personas.predict(scale=r["business_scale"], industry=r["industry"],
                                  cin=r["cin"])
        key = (r["business_scale"], r["industry"], personas.is_llp(r["cin"]))
        shown.setdefault(key, (r["company_name"], titles))
        if not args.dry_run:
            cur.execute(f"""UPDATE {config.CRM_DB_TABLE}
                            SET TargetTitle1=%s, TargetTitle2=%s, TargetTitle3=%s,
                                UpdatedAt=SYSUTCDATETIME()
                            WHERE Website=%s""", (*titles, r["domain"]))
            written += 1
    if not args.dry_run:
        conn.commit()

    print(f"{'would write' if args.dry_run else 'wrote'} "
          f"{len(need) if args.dry_run else written} row(s)\n")
    print("one example per (scale, industry, entity) combination present:")
    for (scale, industry, llp), (name, titles) in sorted(
            shown.items(), key=lambda kv: str(kv[0]))[:14]:
        ent = "LLP" if llp else "PvtLtd"
        print(f"  {str(scale):<11} {str(industry)[:26]:<27} {ent:<7} {' | '.join(titles)}")
    conn.close()


if __name__ == "__main__":
    main()
