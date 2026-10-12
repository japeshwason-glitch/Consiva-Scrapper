"""
The CRM (SQL Server), READ-ONLY. Every statement goes through assert_read_only();
the reader has no write methods, and the CRM team owns the AE_ columns.

CrmReader is what intake talks to. Tests give intake an in-memory fake with the
same methods.
"""
import re

import config

from . import settings


class NotReadOnly(Exception):
    pass


_WRITE_WORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|EXEC|EXECUTE|GRANT|REVOKE|"
    r"DENY|BACKUP|RESTORE|SHUTDOWN|BULK|OPENROWSET|OPENQUERY)\b|\bINTO\b|;", re.I)


def assert_read_only(sql):
    """Raises NotReadOnly unless `sql` is a single SELECT (or WITH ... SELECT) with no write keyword."""
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I) or _WRITE_WORDS.search(sql.rstrip().rstrip(";")):
        raise NotReadOnly(f"refusing to send to the CRM: {sql.strip()[:80]!r}")


def connect():
    import pymssql
    missing = [k for k in ("CRM_DB_HOST", "CRM_DB_NAME", "CRM_DB_USER", "CRM_DB_PASSWORD")
               if not getattr(config, k)]
    if missing:
        raise RuntimeError(f"missing CRM settings: {', '.join(missing)}")
    return pymssql.connect(server=config.CRM_DB_HOST, port=str(config.CRM_DB_PORT),
                           user=config.CRM_DB_USER, password=config.CRM_DB_PASSWORD,
                           database=config.CRM_DB_NAME, timeout=60, login_timeout=20)


def _chunks(items, n):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


class CrmReader:
    """Read-only view of dbo.CRM_Scrapper and its contact, activity and event tables."""

    CHUNK = 500       # ids per IN (...) list; SQL Server allows 2,100 parameters

    def __init__(self, conn=None):
        self._conn = conn or connect()
        self.c = settings.CRM

    def close(self):
        self._conn.close()

    def _select(self, sql, params=()):
        assert_read_only(sql)
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return cur.fetchall()

    def _source_filter(self):
        sources = settings.ALLOWED_SOURCES
        marks = ", ".join(["%s"] * len(sources))
        return f"{self.c['source']} IN ({marks})", list(sources)

    def companies_after(self, after_id=0):
        """Every company of an allowed source above `after_id`, with both status columns."""
        c = self.c
        where, params = self._source_filter()
        rows = self._select(
            f"SELECT {c['company_id']}, {c['website']}, {c['scrape_status']}, {c['report_status']}, "
            f"{c['company_scale']}, {c['company_name']}, {c['industry']} FROM {c['companies']} WHERE {where} AND {c['company_id']} > %s "
            f"ORDER BY {c['company_id']}", [*params, after_id])
        return [{"id": r[0], "website": r[1], "scrape_status": r[2], "report_status": r[3],
                 "company_scale": r[4], "company_name": r[5], "industry": r[6]} for r in rows]

    def report_rows(self, company_ids):
        """Companies that already carry a generated report: what adoption reads. [{id, website, ..., report_url, ...}]"""
        c = self.c
        where, params = self._source_filter()
        marks = ", ".join(["%s"] * len(company_ids))
        rows = self._select(
            f"SELECT {c['company_id']}, {c['website']}, {c['company_name']}, {c['company_scale']}, {c['industry']}, "
            f"{c['gap_score']}, {c['report_url']}, {c['report_status']}, {c['report_generated']} "
            f"FROM {c['companies']} WHERE {where} AND {c['company_id']} IN ({marks})", [*params, *company_ids])
        return [{"id": r[0], "website": r[1], "company_name": r[2], "company_scale": r[3], "industry": r[4],
                 "gap_score": r[5], "report_url": r[6], "report_status": r[7], "report_generated": r[8]}
                for r in rows]

    def status_columns(self, company_ids):
        """{id: (ScrapeStatus, ReportStatus)} as the CRM holds them now, for the mirror audit."""
        c, out = self.c, {}
        for chunk in _chunks(company_ids, self.CHUNK):
            marks = ", ".join(["%s"] * len(chunk))
            rows = self._select(
                f"SELECT {c['company_id']}, {c['scrape_status']}, {c['report_status']} "
                f"FROM {c['companies']} WHERE {c['company_id']} IN ({marks})", chunk)
            out.update({r[0]: (r[1], r[2]) for r in rows})
        return out

    def contacts(self, company_ids):
        """Non-deleted contacts that have an email, for these companies."""
        c, out = self.c, []
        for chunk in _chunks(company_ids, self.CHUNK):
            marks = ", ".join(["%s"] * len(chunk))
            rows = self._select(
                f"SELECT CAST({c['contact_id']} AS varchar(36)), {c['contact_company_id']}, "
                f"{c['contact_email']}, {c['contact_title']}, {c['contact_first_name']} FROM {c['contacts']} "
                f"WHERE {c['contact_deleted']} = 0 AND {c['contact_company_id']} IN ({marks}) "
                f"AND {c['contact_email']} IS NOT NULL AND LTRIM(RTRIM({c['contact_email']})) <> ''",
                chunk)
            out += [{"contact_id": r[0].lower(), "company_id": r[1], "email": r[2], "job_title": r[3],
                     "first_name": (r[4] or "").strip() or None} for r in rows]
        return out

    def emailed_company_ids(self, company_ids):
        """Which of these companies already have an Email activity in the CRM."""
        c, out = self.c, set()
        for chunk in _chunks(company_ids, self.CHUNK):
            marks = ", ".join(["%s"] * len(chunk))
            rows = self._select(
                f"SELECT DISTINCT {c['activity_company_id']} FROM {c['activities']} "
                f"WHERE {c['activity_type']} = %s AND {c['activity_deleted']} = 0 "
                f"AND {c['activity_company_id']} IN ({marks})",
                [settings.EMAIL_ACTIVITY_TYPE, *chunk])
            out |= {r[0] for r in rows}
        return out

    def bounced_emails(self):
        """Every address with a Bounce event, from both CRM event tables, lowercased."""
        c, out = self.c, set()
        for table in (c["company_events"], c["campaign_events"]):
            rows = self._select(
                f"SELECT DISTINCT LOWER(LTRIM(RTRIM({c['event_email']}))) FROM {table} "
                f"WHERE {c['event_type']} = %s", [settings.BOUNCE_EVENT_TYPE])
            out |= {r[0] for r in rows if r[0]}
        return out
