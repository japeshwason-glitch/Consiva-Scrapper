"""
Rewrites GapScore in SQL Server onto the inverted scale (100 = clean).

    python backfill_gap_polarity.py --dry-run
    python backfill_gap_polarity.py

Deliberately NOT "UPDATE ... SET GapScore = 100 - GapScore". That is correct
exactly once; run it twice and every score is back where it started with
nothing to show which way round the table is. Instead each row is recomputed
from our own consent_gap_score, which is the source of truth and never
changes polarity -- so this is safe to re-run any number of times.

CRM_Scrapper rows are matched on Website, the only key the two tables share.
crm.CrmLeads is not touched here: its analysis lives only in SQL Server, so it
is corrected by re-running inbound_analyse.py --force, which recomputes from
the site itself.
"""
import argparse

import config
import crm_push
import db


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with db.get_cursor() as cur:
        cur.execute("""SELECT COALESCE(final_domain, resolved_domain) AS domain,
                              consent_gap_score
                       FROM companies
                       WHERE delivered_at IS NOT NULL
                         AND consent_gap_score IS NOT NULL""")
        ours = {r["domain"]: r["consent_gap_score"] for r in cur.fetchall()}
    print(f"{len(ours)} delivered compan(ies) in our database")

    conn = crm_push.connect()
    cur = conn.cursor()
    cur.execute(f"SELECT Website, GapScore FROM {config.CRM_DB_TABLE}")
    theirs = cur.fetchall()
    print(f"{len(theirs)} row(s) in {config.CRM_DB_TABLE}\n")

    planned, unmatched, already = [], [], 0
    for website, current in theirs:
        internal = ours.get(website)
        if internal is None:
            unmatched.append((website, current))
            continue
        target = crm_push.outward_gap_score(internal)
        if current == target:
            already += 1
            continue
        planned.append((website, current, target, internal))

    print(f"  {len(planned)} row(s) to rewrite")
    print(f"  {already} already on the new scale")
    print(f"  {len(unmatched)} row(s) with no match in our database")
    for w, c in unmatched[:5]:
        print(f"      no match: {w} (GapScore {c})")

    for w, c, t, internal in planned[:6]:
        print(f"      {w:<30} {c:>3} -> {t:>3}   (our gap {internal})")
    if len(planned) > 6:
        print(f"      ... and {len(planned) - 6} more")

    if args.dry_run:
        print("\ndry run -- nothing written")
        return
    if not planned:
        print("\nnothing to do")
        return

    for website, _current, target, _internal in planned:
        cur.execute(f"""UPDATE {config.CRM_DB_TABLE}
                        SET GapScore = %s, UpdatedAt = SYSUTCDATETIME()
                        WHERE Website = %s""", (target, website))
    conn.commit()

    cur.execute(f"SELECT MIN(GapScore), MAX(GapScore), COUNT(*) FROM {config.CRM_DB_TABLE}")
    lo, hi, n = cur.fetchone()
    cur.execute(f"SELECT COUNT(*) FROM {config.CRM_DB_TABLE} WHERE GapScore < 100")
    leads = cur.fetchone()[0]
    conn.close()
    print(f"\nrewrote {len(planned)} row(s)")
    print(f"GapScore now {lo}..{hi} across {n} rows; {leads} are leads (GapScore < 100)")


if __name__ == "__main__":
    main()
