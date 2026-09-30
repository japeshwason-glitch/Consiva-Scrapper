"""
Copy the partner-import rows out of the CRM into a local JSON file for /partner.

    python partner_snapshot.py [path]     # default /tmp/partner_snapshot.json

The /partner page used to query the CRM's SQL Server on every request. Twice
that wedged the whole API -- every worker thread parked on a lock, health
still green -- while the CRM connection was flaky. The API now only reads this
file, and runs this script as a separate process under a hard timeout: a CRM
that hangs costs a stale snapshot, never a frozen page.

About 3,500 rows, so the whole set is copied and the API filters in memory.
Contact names and emails are never selected.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import crm_push
import partner_import_analyse as pia

DEFAULT_PATH = "/tmp/partner_snapshot.json"
FINDING_CHARS = 300          # the page shows 140; the rest is headroom

COLS = ("id", "company", "website", "status", "attempts", "scraped_at",
        "gap_score", "trackers", "third_parties", "banner", "scale",
        "industry", "finding")


def fetch():
    conn = crm_push.connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT Id, CompanyName, Website, {pia.QUEUE_LABEL}, ScrapeAttempts,
                   ScrapedAt, GapScore, TrackersPreConsent, ThirdPartiesCount,
                   CookieBanner, CompanyScale, Industry,
                   LEFT(ConsentFinding, {FINDING_CHARS})
            FROM {pia.TABLE} WHERE Source = %s""", (pia.SOURCE,))
        rows = [dict(zip(COLS, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        if r["scraped_at"]:
            r["scraped_at"] = r["scraped_at"].isoformat() + "Z"
    return rows


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    snap = {"taken_at": datetime.now(timezone.utc).isoformat(), "rows": fetch()}
    # Write-then-rename, so a reader never sees half a file.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, path)
    print(f"{len(snap['rows'])} partner rows -> {path}")


if __name__ == "__main__":
    main()
