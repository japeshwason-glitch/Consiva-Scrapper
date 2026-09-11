"""
In-container scheduler. Runs the daily pipeline at RUN_AT, then sleeps.

Replaces systemd inside Docker, and keeps the same power-cut behaviour:

* On start it checks whether today's target is already met. If the box was off
  through the scheduled window, the run happens immediately on boot rather than
  being skipped for the day -- the same thing systemd's Persistent=true does.
* Progress is counted from the database, never held here, so restarting this
  process mid-run resumes toward the target instead of starting over.
* A crashed run does not stop the loop; the next window tries again, and
  Docker's restart policy covers the process dying outright.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import db

RUN_AT = os.environ.get("RUN_AT", "02:00")
TARGET = int(os.environ.get("DAILY_LEAD_TARGET", "500"))



def leads_today():
    with db.get_cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS n FROM companies
                       WHERE delivered_at::date = CURRENT_DATE""")
        return cur.fetchone()["n"]


def next_run(now):
    hour, minute = (int(x) for x in RUN_AT.split(":"))
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today > now else today + timedelta(days=1)


def run_once():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] starting daily run "
          f"(target {TARGET} contactable leads, no scrape cap)", flush=True)
    result = subprocess.run(
        [sys.executable, "-u", "run_daily.py",
         "--target", str(TARGET)],
        cwd=os.path.dirname(os.path.abspath(__file__)))
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] run finished rc={result.returncode}, "
          f"{leads_today()} leads delivered today", flush=True)


def main():
    print(f"scheduler up — daily at {RUN_AT}, target {TARGET} contactable leads",
          flush=True)

    # Catch-up on boot: if today's window has passed and the target is not met,
    # the machine was probably off for it.
    now = datetime.now()
    hour, minute = (int(x) for x in RUN_AT.split(":"))
    window_passed = now > now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if window_passed and leads_today() < TARGET:
        print("today's window already passed and the target is not met — "
              "running now (catch-up)", flush=True)
        run_once()

    while True:
        upcoming = next_run(datetime.now())
        sleep_for = (upcoming - datetime.now()).total_seconds()
        print(f"next run {upcoming:%Y-%m-%d %H:%M} "
              f"(in {sleep_for/3600:.1f}h)", flush=True)
        # Wake hourly rather than sleeping the whole gap, so a clock change or a
        # long suspend cannot leave this parked for a day.
        while datetime.now() < upcoming:
            time.sleep(min(3600, max(30, (upcoming - datetime.now()).total_seconds())))
        run_once()


if __name__ == "__main__":
    main()
