"""
Every write to the CRM goes through this file, and update_company() is the only
function in the repo's campaign package that sends one.

What it allows, each by name:
  * the two status columns, ScrapeStatus and ReportStatus, with only the values the table
    already uses (settings.VOCAB);
  * the nine report columns the retired report job filled (REPORT_COLUMNS), each checked against
    the format of the legacy rows (FORMATS); and
  * the AE_ draft columns the CRM team created (settings.AE_COLUMNS).
It refuses, before it opens a connection:
  * any write at all unless the connected Postgres database is the configured primary (settings.PRIMARY_DATABASE);
  * AE_DraftStatus ready/risky together with a placeholder (or missing) AE_DraftVersion;
  * any other column, any value outside the existing vocabulary, any value in the wrong format;
  * a company that is a test company, a legacy company, or not in ae_companies at all;
and, once connected:
  * a row that is not in the CRM (any Source is allowed, a NULL one too: the gate is ae_companies, below).

ae_companies is the authority for "is this a test company", never an argument: a caller
cannot talk its way past the guard.
"""
import re

import config
import db

from . import crm_read, settings

# What the retired report job wrote on a completed scan or a report, now written by this
# pipeline. Each is allowed by NAME and checked against the format the legacy rows have
# (docs: the legacy `generated` / `no_gap` rows were compared column by column).
SCAN_COLUMNS = ("GapScore", "TrackersPreConsent", "ThirdPartiesCount",
                "CookieBanner", "CollectsViaForm", "ConsentFinding")
REPORT_COLUMNS = (*SCAN_COLUMNS, "UpdatedAt", "DownloadReportUrl", "ReportGeneratedAt")


class _ServerNow:
    """UpdatedAt and ReportGeneratedAt are stamped by the CRM server (SYSUTCDATETIME()), as the old job did."""
    def __repr__(self):
        return "SERVER_NOW"


SERVER_NOW = _ServerNow()
SERVER_TIME_COLUMNS = ("UpdatedAt", "ReportGeneratedAt", "AE_DraftGeneratedAt")


class CrmWriteRefused(Exception):
    """The write was refused by a guard; nothing was sent to the CRM."""


class TestCompanyRefused(CrmWriteRefused):
    __test__ = False        # not a test class, whatever a test runner thinks of the name


class CrmWriteFailed(Exception):
    """The CRM did not take the write; the caller must treat its step as failed."""


def report_url_re():
    return re.compile(r"^" + re.escape(config.REPORT_PUBLIC_BASE_URL) + r"/r/[0-9a-f]{32}\.pdf$")


def _nonneg_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _valid_collects_via_form(v):
    # NULL, or "<fields> — no consent checkbox" / "<fields> — consent checkbox present", optionally
    # followed by "; submitted to <hosts>" (crm_push.form_sentence). Legacy maximum is 109 characters.
    return v is None or (isinstance(v, str) and 0 < len(v) <= 1000 and " — " in v
                         and bool(re.search(r"consent checkbox( present)?(;|$)", v)))


def _valid_consent_finding(v):
    # build_consent_summary(): "; "-joined facts ending in a full stop. Legacy maximum is 639 characters.
    return isinstance(v, str) and 0 < len(v) <= 4000 and v.endswith(".") and "\n" not in v


# column -> validator. A column is allowed only if it is named here, in settings.VOCAB or in AE_COLUMNS.
FORMATS = {
    "GapScore": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100,
    "TrackersPreConsent": _nonneg_int,
    "ThirdPartiesCount": _nonneg_int,
    "CookieBanner": lambda v: v in ("present", "none"),
    "CollectsViaForm": _valid_collects_via_form,
    "ConsentFinding": _valid_consent_finding,
    "DownloadReportUrl": lambda v: v is None or (isinstance(v, str) and len(v) <= 1000
                                                  and report_url_re().match(v) is not None),
    "UpdatedAt": lambda v: v is SERVER_NOW,
    "ReportGeneratedAt": lambda v: v is SERVER_NOW,
    # The AE_ draft columns (types as the CRM team created them): band char(1), body nvarchar(max), email
    # nvarchar(255), status nvarchar(20), subject nvarchar(255), version nvarchar(20), generated-at datetime2.
    # AE_DraftContactId is an int in the CRM, which no contact id of ours is (they are GUIDs): not written.
    "AE_DraftBand": lambda v: v in ("A", "B", "C", "D"),
    "AE_DraftBody": lambda v: isinstance(v, str) and 0 < len(v) <= 100_000,
    "AE_DraftEmail": lambda v: isinstance(v, str) and len(v) <= 255 and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", v) is not None,
    "AE_DraftStatus": lambda v: v in ("ready", "risky", "expired"),
    "AE_DraftSubject": lambda v: isinstance(v, str) and 0 < len(v) <= 255 and "\n" not in v,
    "AE_DraftVersion": lambda v: isinstance(v, str) and 0 < len(v) <= 20,
    "AE_DraftGeneratedAt": lambda v: v is SERVER_NOW,
}


def _connect():
    conn = crm_read.connect()
    if settings.EXPECT_CRM_DATABASE:
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME()")
        actual = cur.fetchone()[0]
        if actual != settings.EXPECT_CRM_DATABASE:
            conn.close()
            raise CrmWriteRefused(f"connected to CRM database {actual!r}, expected {settings.EXPECT_CRM_DATABASE!r} "
                                  f"(AE_EXPECT_CRM_DATABASE); nothing was written")
    return conn


def _require_primary_database():
    """
    Refuses unless the Postgres database we are actually connected to (asked of Postgres, not read from a
    setting) is settings.PRIMARY_DATABASE. Checked before anything else, so a throwaway, test or demo database
    can never drive a real CRM write.
    """
    with db.get_cursor() as cur:
        cur.execute("SELECT current_database() AS name")
        connected = cur.fetchone()["name"]
    if connected != settings.PRIMARY_DATABASE:
        raise CrmWriteRefused(f"connected to Postgres database {connected!r}, not the primary "
                              f"{settings.PRIMARY_DATABASE!r}; a CRM write is only allowed from the primary database")


def _check_placeholder(columns):
    """A draft marked ready or risky must come with a template version that is not a placeholder."""
    if columns.get("AE_DraftStatus") in ("ready", "risky"):
        version = columns.get("AE_DraftVersion")
        if not version or "placeholder" in version.lower():
            raise CrmWriteRefused("AE_DraftStatus ready/risky needs an AE_DraftVersion that is not a placeholder "
                                  f"(got {version!r}); write placeholder drafts as 'expired'")


def _allowed_columns():
    return {settings.CRM["scrape_status"], settings.CRM["report_status"],
            *REPORT_COLUMNS, *settings.AE_COLUMNS}


def _check_columns(columns):
    if not columns:
        raise CrmWriteRefused("no columns to write")
    refused = sorted(set(columns) - _allowed_columns())
    if refused:
        raise CrmWriteRefused(f"columns not allowed: {refused}; this pipeline may write only "
                              f"{settings.CRM['scrape_status']}, {settings.CRM['report_status']}, "
                              f"the report columns {list(REPORT_COLUMNS)} and the AE_ draft columns")
    for col, value in columns.items():
        vocab = settings.VOCAB.get(col)
        if vocab is not None and value not in vocab:
            raise CrmWriteRefused(f"{col}={value!r} is not a value this pipeline writes "
                                  f"(allowed: {sorted(vocab)})")
        check = FORMATS.get(col)
        if check is not None and not check(value):
            raise CrmWriteRefused(f"{col}={str(value)[:80]!r} is not in the format the CRM holds for this column")


def _guard(company_id):
    """Looks the company up in ae_companies; raises unless it is a real, non-test, non-legacy one."""
    with db.get_cursor() as cur:
        cur.execute("SELECT is_test, status FROM ae_companies WHERE company_id = %s", (company_id,))
        row = cur.fetchone()
    if row is None:
        raise CrmWriteRefused(f"company {company_id} is not in ae_companies; refusing to write it")
    if row["is_test"]:
        raise TestCompanyRefused(f"company {company_id} is a test company; nothing about it reaches the CRM")
    if row["status"] == "legacy":
        raise CrmWriteRefused(f"company {company_id} is a legacy company; it gets no CRM writes")


def update_company(company_id, **columns):
    """
    Writes `columns` to the company's row in the CRM, one statement in one transaction.
    Raises CrmWriteRefused (a guard said no) or CrmWriteFailed (the CRM did not take it).
    """
    _require_primary_database()
    if company_id >= settings.LEAD_ID_BASE:
        return _update_lead(company_id, columns)
    if settings.NO_SCRAPE_STATUS:                        # a CRM without that column (staging): nothing to write there
        columns = {k: v for k, v in columns.items() if k != settings.CRM["scrape_status"]}
        if not columns:
            return
    _check_columns(columns)
    _check_placeholder(columns)
    _guard(company_id)
    table, id_col, src_col = settings.CRM["companies"], settings.CRM["company_id"], settings.CRM["source"]
    # every name is in the allow-list checked above; server-time columns carry no parameter
    sets = ", ".join(f"{n} = SYSUTCDATETIME()" if v is SERVER_NOW else f"{n} = %s"
                     for n, v in columns.items())
    values = tuple(v for v in columns.values() if v is not SERVER_NOW)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {src_col} FROM {table} WHERE {id_col} = %s", (company_id,))
        row = cur.fetchone()
        if row is None or ("*" not in settings.WRITE_SOURCES and row[0] not in settings.WRITE_SOURCES):
            raise CrmWriteRefused(f"CRM row {company_id} is missing or not a {' / '.join(settings.WRITE_SOURCES)} row "
                                  f"(Source={None if row is None else row[0]!r})")
        cur.execute(f"UPDATE {table} SET {sets} WHERE {id_col} = %s", (*values, company_id))
        if cur.rowcount != 1:
            conn.rollback()
            raise CrmWriteFailed(f"CRM row {company_id}: the update matched {cur.rowcount} rows, expected 1")
        conn.commit()
    except (CrmWriteRefused, CrmWriteFailed):
        raise
    except Exception as exc:                    # a dropped connection, a timeout, a SQL error
        try:
            conn.rollback()
        except Exception:                       # noqa: BLE001 -- the connection may already be gone
            pass
        raise CrmWriteFailed(f"CRM write for company {company_id} failed: {exc}") from exc
    finally:
        conn.close()


def _update_lead(company_id, columns):
    """
    A lead taken from crm.CrmLeads: only the AE_ draft columns are written, to that row (found through
    ae_lead_map), never a deleted row, never a test company. crm.CrmLeads has no status or report columns, so those
    (which the pipeline's status changes carry) are dropped here, not written anywhere.
    """
    columns = {k: v for k, v in columns.items() if k in settings.AE_COLUMNS}
    if not columns:
        return
    _check_columns(columns)
    _check_placeholder(columns)
    _guard(company_id)
    with db.get_cursor() as cur:
        cur.execute("SELECT lead_id FROM ae_lead_map WHERE company_id = %s", (company_id,))
        row = cur.fetchone()
    if row is None:
        raise CrmWriteRefused(f"company {company_id} is not a mapped crm.CrmLeads lead")
    table = settings.CRM_LEADS_TABLE
    sets = ", ".join(f"{n} = SYSUTCDATETIME()" if v is SERVER_NOW else f"{n} = %s" for n, v in columns.items())
    values = tuple(v for v in columns.values() if v is not SERVER_NOW)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT IsDeleted FROM {table} WHERE Id = %s", (row["lead_id"],))
        found = cur.fetchone()
        if found is None or found[0]:
            raise CrmWriteRefused(f"lead {row['lead_id']} is missing or deleted in {table}")
        cur.execute(f"UPDATE {table} SET {sets} WHERE Id = %s AND IsDeleted = 0", (*values, row["lead_id"]))
        if cur.rowcount != 1:
            conn.rollback()
            raise CrmWriteFailed(f"lead {row['lead_id']}: the update matched {cur.rowcount} rows, expected 1")
        conn.commit()
    except (CrmWriteRefused, CrmWriteFailed):
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:                       # noqa: BLE001 -- the connection may already be gone
            pass
        raise CrmWriteFailed(f"CRM write for lead company {company_id} failed: {exc}") from exc
    finally:
        conn.close()


def write_draft(company_id, is_test=None):
    """
    Writes one company's draft into the AE_ columns. Not built yet (a later milestone); the
    guard is. `is_test` exists only so a caller cannot argue with the guard: it is not read.
    """
    _guard(company_id)
    raise NotImplementedError("the CRM draft write is a later milestone")
