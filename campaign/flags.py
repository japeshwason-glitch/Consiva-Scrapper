"""Global flags in ae_flags. The kill switch is one of them."""
import db

KILL_SWITCH = "kill_switch"


def get_flag(flag, default=None):
    with db.get_cursor() as cur:
        cur.execute("SELECT value FROM ae_flags WHERE flag = %s", (flag,))
        row = cur.fetchone()
    return row["value"] if row else default


def set_flag(flag, value):
    with db.get_cursor() as cur:
        cur.execute("""INSERT INTO ae_flags (flag, value) VALUES (%s, %s)
                       ON CONFLICT (flag) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    (flag, value))


def kill_switch_on():
    """
    True when jobs must not run. Fails closed: a missing flag row counts as on,
    so a half-applied migration stops the jobs rather than letting them run
    with no way to stop them.
    """
    return get_flag(KILL_SWITCH, default="on") != "off"
