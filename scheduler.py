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

RUN_AT = os.environ.get("RUN_AT", "06:30")
TARGET = int(os.environ.get("DAILY_LEAD_TARGET", "500"))

# The inbound job runs first, in the same wake-up rather than on a timer of its
# own. It takes minutes against the MCA run's hours, and doing it first means
# people who filled in a form today are analysed before the machine commits to
# a long crawl -- if the night is cut short, the inbound work is already done.
RUN_INBOUND = os.environ.get("RUN_INBOUND", "1") not in ("0", "false", "False", "")
# Two days, not one. Re-running costs nothing because an analysed lead is
# skipped, so the window self-heals a night that was missed entirely.
INBOUND_DAYS = int(os.environ.get("INBOUND_DAYS", "2"))

# Partner-imported rows (dbo.CRM_Scrapper, Source='PartnerImport') run AFTER
# the MCA run, never before it: the MCA run is the target-bearing job and gets
# first claim on the day, and it has been finishing in ~3h, leaving the rest of
# the day idle. The partner job carries its own daily quota
# (PARTNER_DAILY_TARGET, default 500) counted from the rows themselves, so
# calling it again after a restart only does what today still owes.
RUN_PARTNER = os.environ.get("RUN_PARTNER", "1") not in ("0", "false", "False", "")
# Not worth starting the partner job with less time than this before it must stop.
MIN_PARTNER_H = 0.25
# A fixed daily start for the partner job, "HH:MM". It still never overlaps
# the MCA run: this loop is busy while run_daily.py runs, so a partner time
# that falls inside the MCA run starts the moment the MCA run returns. Unset,
# the partner job simply follows the MCA run.
PARTNER_RUN_AT = os.environ.get("PARTNER_RUN_AT", "").strip()



def leads_today():
    with db.get_cursor() as cur:
        cur.execute("""SELECT COUNT(*) AS n FROM companies
                       WHERE delivered_at::date = CURRENT_DATE""")
        return cur.fetchone()["n"]


def at_today(hhmm, now):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_at(hhmm, now):
    today = at_today(hhmm, now)
    return today if today > now else today + timedelta(days=1)


def next_run(now):
    return next_at(RUN_AT, now)


def partner_due(now=None):
    """Whether the partner job may start now. The job itself owns the quota."""
    now = now or datetime.now()
    return not PARTNER_RUN_AT or now >= at_today(PARTNER_RUN_AT, now)


# A run must finish this long before the next window opens. run_daily's own
# 23h cap only fits a run that starts AT the window: a catch-up after a reboot
# at 18:37 ran to 17:37 the next day and swallowed that morning's 06:30 run
# (Sep 27 -> 28, and the same on Sep 23 -> 24).
WINDOW_MARGIN_H = 1.0
# Not worth starting a catch-up that would have less than this to work with.
MIN_RUN_H = 2.0


def hours_until_next_window(now=None):
    now = now or datetime.now()
    return (next_run(now) - now).total_seconds() / 3600


def run_partner():
    """The partner-import batch, bounded to end before the next window."""
    if not RUN_PARTNER or not partner_due():
        return
    budget_h = hours_until_next_window() - WINDOW_MARGIN_H
    if budget_h < MIN_PARTNER_H:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] partner import skipped: "
              f"only {budget_h:.1f}h before the next window", flush=True)
        return
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] partner import "
          f"({budget_h:.1f}h budget)", flush=True)
    here = os.path.dirname(os.path.abspath(__file__))
    rc = subprocess.run(
        [sys.executable, "-u", "partner_import_analyse.py",
         "--max-minutes", f"{budget_h * 60:.0f}"], cwd=here).returncode
    if rc != 0:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] partner import failed "
              f"rc={rc}", flush=True)


def run_once():
    here = os.path.dirname(os.path.abspath(__file__))
    budget_h = hours_until_next_window() - WINDOW_MARGIN_H
    env = dict(os.environ, MAX_RUN_HOURS=f"{min(budget_h, 23):.2f}")

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
    print(f"  must finish by the next window: {min(budget_h, 23):.1f}h budget",
          flush=True)
    result = subprocess.run(
        [sys.executable, "-u", "run_daily.py",
         "--target", str(TARGET)], cwd=here, env=env)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] run finished rc={result.returncode}, "
          f"{leads_today()} leads delivered today", flush=True)
    run_partner()


def main():
    print(f"scheduler up — daily at {RUN_AT}, target {TARGET} leads"
          + (f", inbound first over {INBOUND_DAYS} days" if RUN_INBOUND
             else ", inbound disabled")
          + ((f", partner import at {PARTNER_RUN_AT}" if PARTNER_RUN_AT else ", partner import after") if RUN_PARTNER else ""), flush=True)

    # Catch-up on boot: if today's window has passed and the target is not met,
    # the machine was probably off for it.
    now = datetime.now()
    hour, minute = (int(x) for x in RUN_AT.split(":"))
    window_passed = now > now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if window_passed and leads_today() < TARGET:
        left = hours_until_next_window(now) - WINDOW_MARGIN_H
        if left >= MIN_RUN_H:
            print("today's window already passed and the target is not met — "
                  f"running now (catch-up, {left:.1f}h until it must stop)", flush=True)
            run_once()
        else:
            print(f"today's window passed, but only {left:.1f}h remain before the "
                  f"next one -- skipping the catch-up", flush=True)
    elif window_passed:
        # MCA already met its target today; the partner job may still owe work.
        run_partner()

    while True:
        now = datetime.now()
        upcoming = next_run(now)
        wake = upcoming
        if RUN_PARTNER and PARTNER_RUN_AT:
            wake = min(upcoming, next_at(PARTNER_RUN_AT, now))
        sleep_for = (wake - datetime.now()).total_seconds()
        what = "run" if wake == upcoming else "partner import"
        print(f"next {what} {wake:%Y-%m-%d %H:%M} "
              f"(in {sleep_for/3600:.1f}h)", flush=True)
        # Wake hourly rather than sleeping the whole gap, so a clock change or a
        # long suspend cannot leave this parked for a day.
        while datetime.now() < wake:
            time.sleep(min(3600, max(30, (wake - datetime.now()).total_seconds())))
        if wake == upcoming:
            run_once()
        else:
            run_partner()


if __name__ == "__main__":
    main()
