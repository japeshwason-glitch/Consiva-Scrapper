"""
Re-classify stored pre-consent cookies with the fixed rules and recompute every
finding built from them -- no rescan. One-off, for the 2026-10-cookie-fix.

    python scripts/reclassify_cookies.py            # dry run: report only
    python scripts/reclassify_cookies.py --apply    # back up, then write

Every MCA row stores its full cookie records (pre_consent_cookies), so the new
categories can be applied offline. The other score inputs are read from the
row: has_cmp / cmp_name, has_cookie_banner, has_privacy_policy and the form
columns. The one input with no column -- whether a collecting form carried a
privacy link -- is read back from the stored summary, which states it.

Before trusting that reconstruction, each row is replayed under the OLD rules
and must reproduce its stored score and summary exactly; the match rate is
printed. Rows that do not replay are reported, and still recomputed.

Writes through db.update_fields(): status, retry_count, last_error and
updated_at are never touched. Idempotent -- a second run changes nothing, so it
can be re-run after the scheduler image is rebuilt to catch rows scanned in
between.
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stages"))

import db
import fingerprints as fp

s3 = import_module("03_scrape_playwright")

COLUMNS = ("pre_consent_cookies", "tracking_cookie_count", "third_party_count",
           "third_party_domains", "max_cookie_lifetime_days", "consent_gap_score",
           "consent_gap_summary")

OLD_TRACKING = ("advertising", "session_replay", "analytics", "third_party")
NEW_EXCLUDED = ("necessary", "unclassified")


# --- the rules as they were, for the replay check only ------------------------
def legacy_analysis(records):
    """summarise_records() before the fix, from the categories stored at scan time."""
    tracking = [r for r in records if r["category"] in OLD_TRACKING]
    lifetimes = [r["lifetime_days"] for r in tracking if r["lifetime_days"]]
    tp = sorted({r["domain"] for r in records if r["party"] == "third"})
    return {
        "records": records, "tracking": tracking, "cookies_total": len(records),
        "tracking_count": len(tracking), "third_party_domains": tp,
        "third_party_count": len(tp),
        "max_lifetime_days": max(lifetimes) if lifetimes else None,
        "long_lived_count": sum(1 for d in lifetimes if d > 180),
        "by_category": {c: [r["name"] for r in records if r["category"] == c]
                        for c in OLD_TRACKING},
    }


def legacy_consent_score(has_cmp, a, banner, policy, form):
    if has_cmp:
        return fp.ALIGNED
    base = 0
    if a["tracking"]:
        c = a["by_category"]
        w = 3.0 * len(c["advertising"]) + 3.0 * len(c["session_replay"]) \
            + 2.0 * len(c["third_party"]) + 1.5 * len(c["analytics"])
        s = min(40.0, w) + min(15.0, 3.0 * a["third_party_count"]) \
            + min(10.0, 2.0 * a["long_lived_count"])
        s += (0 if banner else 25) + (0 if policy else 15)
        s += (10 if c["session_replay"] else 0) + (5 if c["advertising"] else 0)
        base = int(min(100, round(s)))
    if not form["has_data_form"]:
        return fp.ALIGNED - base
    extra = (0 if form["consent_checkbox"] else 25) \
        + (10 if not form["privacy_link"] and not policy else 0) \
        + (10 if form["external_hosts"] else 0)
    return fp.ALIGNED - int(min(100, base + extra))


# --- reconstruction ----------------------------------------------------------
def form_from_row(row):
    """form_gap()'s output, rebuilt from the columns plus the stored summary."""
    if not row["has_data_form"]:
        return {"has_data_form": False, "forms_collecting": 0, "field_categories": [],
                "consent_checkbox": None, "privacy_link": None, "external_hosts": []}
    summary = row["consent_gap_summary"] or ""
    m = re.search(r"collects personal data via (\d+) web form\(s\)", summary)
    return {
        "has_data_form": True,
        "forms_collecting": int(m.group(1)) if m else 1,
        "field_categories": row["form_field_types"] or [],
        "consent_checkbox": row["form_consent_checkbox"],
        "privacy_link": "and no privacy link on the form" not in summary,
        "external_hosts": row["form_third_party_hosts"] or [],
    }


def verdict(has_cmp, score):
    if has_cmp:
        return "has_cmp"
    if fp.is_severe_score(score):
        return "severe"
    return "moderate" if fp.is_lead_score(score) else "clean"


def recompute(row):
    old_records = row["pre_consent_cookies"] or []
    banner, policy, has_cmp = row["has_cookie_banner"], row["has_privacy_policy"], row["has_cmp"]
    form = form_from_row(row)
    embed = row["form_embed_provider"]

    old_a = legacy_analysis(old_records)
    replay_ok = (
        legacy_consent_score(has_cmp, old_a, banner, policy, form) == row["consent_gap_score"]
        and s3.build_consent_summary(row["cmp_name"], old_a, banner, policy, form, embed)
        == row["consent_gap_summary"]
    )

    new_records = [{**r, "category": fp.cookie_category(r["name"], r["domain"],
                                                         r["party"] == "third")}
                   for r in old_records]
    a = fp.summarise_records(new_records)
    new = {
        "pre_consent_cookies": new_records,
        "tracking_cookie_count": a["tracking_count"],
        "third_party_count": a["third_party_count"],
        "third_party_domains": a["third_party_domains"],
        "max_cookie_lifetime_days": a["max_lifetime_days"],
        "consent_gap_score": fp.consent_score(has_cmp, a, banner, policy, form),
        "consent_gap_summary": s3.build_consent_summary(
            row["cmp_name"], a, banner, policy, form, embed),
    }

    causes, moved = set(), []
    for o, n in zip(old_records, new_records):
        oc, nc = o["category"], n["category"]
        if oc in OLD_TRACKING and nc == "necessary":
            causes.add("necessary removed")
        elif oc == "third_party" and nc == "unclassified":
            causes.add("unclassified excluded")
        elif oc in OLD_TRACKING and nc not in fp.TRACKING_CATEGORIES:
            causes.add("exact-match change")
        if oc in OLD_TRACKING and nc in NEW_EXCLUDED:
            moved.append((n["name"], nc))
    return new, replay_ok, causes, moved


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="back up, then write")
    args = ap.parse_args()

    rows = []
    for status in db.count_by_status():
        rows += db.get_pending(status, limit=10_000_000,
                               extra_where="pre_consent_cookies IS NOT NULL")
    print(f"{len(rows)} rows with stored cookie records "
          f"(rules {fp.FINGERPRINTS_VERSION})\n")

    before, after = Counter(), Counter()
    changed_rows, replay_fail, bucket_moves = [], [], Counter()
    cause_rows, cause_combo, moved_names = Counter(), Counter(), Counter()
    for row in rows:
        new, replay_ok, causes, moved = recompute(row)
        if not replay_ok:
            replay_fail.append(row["cin"])
        vb = verdict(row["has_cmp"], row["consent_gap_score"])
        va = verdict(row["has_cmp"], new["consent_gap_score"])
        before[vb] += 1
        after[va] += 1
        moved_names.update(moved)
        if any(new[k] != row[k] for k in COLUMNS):
            changed_rows.append((row, new))
        if vb != va:
            bucket_moves[f"{vb} -> {va}"] += 1
            for c in causes:
                cause_rows[c] += 1
            cause_combo[" + ".join(sorted(causes)) or "(none)"] += 1

    print(f"replay of old rules reproduces stored score AND summary: "
          f"{len(rows) - len(replay_fail)}/{len(rows)}")
    if replay_fail:
        print(f"  did not replay ({len(replay_fail)}): {', '.join(replay_fail[:10])}"
              + (" ..." if len(replay_fail) > 10 else ""))

    print(f"\n{'verdict':<10} {'before':>7} {'after':>7}")
    for v in ("severe", "moderate", "clean", "has_cmp"):
        print(f"{v:<10} {before[v]:>7} {after[v]:>7}")
    print(f"{'total':<10} {sum(before.values()):>7} {sum(after.values()):>7}")

    moved = sum(bucket_moves.values())
    print(f"\n{moved} rows changed bucket:")
    for k, n in bucket_moves.most_common():
        print(f"  {k:<22} {n}")
    print("by cause (a row can have more than one):")
    for k, n in cause_rows.most_common():
        print(f"  {k:<22} {n}")
    print("by cause combination:")
    for k, n in cause_combo.most_common():
        print(f"  {k:<50} {n}")

    print("\ntop 20 cookie names newly marked necessary / unclassified (cookie count):")
    for (name, cat), n in moved_names.most_common(20):
        print(f"  {n:>6}  {name:<36} {cat}")

    print(f"\n{len(changed_rows)} rows have at least one changed column")
    if not args.apply:
        print("dry run -- nothing written. Re-run with --apply.")
        return

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup = ROOT / "data" / f"companies_consent_backup_{stamp}.json"
    backup.write_text(json.dumps({
        "taken_at": datetime.now().astimezone().isoformat(),
        "rules_before": "pre-2026-10-cookie-fix",
        "columns": list(COLUMNS),
        "rows": [{"cin": r["cin"], "status": r["status"], **{k: r[k] for k in COLUMNS}}
                 for r in rows],
    }, ensure_ascii=False, default=str))
    print(f"backed up {len(rows)} rows -> {backup}")

    for i, (row, new) in enumerate(changed_rows, 1):
        db.update_fields(row["cin"], **{k: new[k] for k in COLUMNS if new[k] != row[k]})
        if i % 1000 == 0:
            print(f"  ... {i}/{len(changed_rows)}", flush=True)
    print(f"wrote {len(changed_rows)} rows (status untouched)")


if __name__ == "__main__":
    main()
