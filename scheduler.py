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

# The inbound job runs first, in the same wake-up rather than on a timer of its
# own. It takes minutes against the MCA run's hours, and doing it first means
# people who filled in a form today are analysed before the machine commits to
# a long crawl -- if the night is cut short, the inbound work is already done.
RUN_INBOUND = os.environ.get("RUN_INBOUND", "1") not in ("0", "false", "False", "")
# Two days, not one. Re-running costs nothing because an analysed lead is
# skipped, so the window self-heals a night that was missed entirely.
INBOUND_DAYS = int(os.environ.get("INBOUND_DAYS", "2"))



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
    here = os.path.dirname(os.path.abspath(__file__))

    if RUN_INBOUND:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] inbound leads "
              f"(last {INBOUND_DAYS} days)", flush=True)
        # Never allowed to stop the night: the MCA run is the target-bearing
        # job, and a failure here must not cost it.
        rc = subprocess.run(
            [sys.executable, "-u", "inbound_analyse.py",
             "--days", str(INBOUND_DAYS)], cwd=here).returncode
        if rc != 0:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] inbound failed rc={rc}, "
                  f"continuing to the MCA run", flush=True)

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] starting daily run "
          f"(target {TARGET} leads, no scrape cap)", flush=True)
    result = subprocess.run(
        [sys.executable, "-u", "run_daily.py",
         "--target", str(TARGET)], cwd=here)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] run finished rc={result.returncode}, "
          f"{leads_today()} leads delivered today", flush=True)


def main():
    print(f"scheduler up — daily at {RUN_AT}, target {TARGET} leads"
          + (f", inbound first over {INBOUND_DAYS} days" if RUN_INBOUND
             else ", inbound disabled"), flush=True)

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
