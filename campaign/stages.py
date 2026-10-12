"""
The nine stages, in pipeline order, with the status moves each one makes. Only stage 1
(intake) is real in milestone 1; stages 2-9 are stubs that declare their transitions
(tests check every one against status.TRANSITIONS) and raise NotImplementedError if run.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import settings, status


@dataclass(frozen=True)
class Stage:
    number: int
    name: str
    summary: str
    transitions: tuple = field(default_factory=tuple)      # ((from_status, to_status), ...)
    fn: Optional[Callable] = None

    @property
    def implemented(self):
        return self.fn is not None

    def run(self, *args, **kwargs):
        if not self.implemented:
            raise NotImplementedError(f"stage {self.number} ({self.name}) is not built yet")
        return self.fn(*args, **kwargs)


def _lazy(module, attr="run"):
    """A stage function imported on first use, so importing this table never loads Playwright or boto3."""
    def call(*args, **kwargs):
        import importlib
        return getattr(importlib.import_module(f"campaign.{module}"), attr)(*args, **kwargs)
    call.__name__ = f"{module}.{attr}"
    return call


STAGES = (
    Stage(1, "intake", "every PartnerImport company not yet in ae_companies (legacy ones imported once)",
          (("pending", "no_website"), ("pending", "held_already_contacted"), ("legacy", "pending")),
          _lazy("intake", "run_intake")),
    Stage(2, "free_checks",
          "syntax, MX, disposable, role addresses, free-mail domains, ae_suppression; "
          "no passing contact -> never scanned",
          (("pending", "checked"), ("pending", "skipped_invalid")), _lazy("checks")),
    Stage(3, "domain", "the Website's domain, else the contacts' shared email domain; duplicates held",
          (("checked", "no_website"), ("checked", "held_duplicate_domain")), _lazy("domains")),
    Stage(4, "scan", "gap analysis of the homepage, 2 retries; ScrapeStatus / ReportStatus mirrored to the CRM",
          (("checked", "scanned"), ("checked", "scan_failed"), ("checked", "unscannable")), _lazy("scan_stage")),
    Stage(5, "score_gate", "score 0-90 and at least 1 gap passes; consent columns written to the CRM",
          (("scanned", "gate_passed"), ("scanned", "score_too_high"), ("scanned", "no_gaps")), _lazy("gate")),
    Stage(6, "report", "one-page report, only for companies that passed the gate; URL written to the CRM",
          (("gate_passed", "reported"),), _lazy("report_stage")),
    Stage(7, "rank_contacts", "title rules, then the local model for the rest, cached per title",
          (("reported", "contact_ranked"), ("reported", "held_no_contact")), _lazy("ranking")),
    Stage(8, "verify", "NeverBounce on the top contact, up to 2 fallbacks; contact and company marked",
          (("contact_ranked", "verified"), ("contact_ranked", "suppressed")), _lazy("verify")),
    Stage(9, "draft", "the band template filled in with two validated sentences; stored in ae_drafts, never written to the CRM",
          (("verified", "drafted"), ("verified", "suppressed")), _lazy("drafting")),
    Stage(10, "write_drafts", "the draft written into the CRM AE_ columns; only when asked for (--write-ae)",
          (("drafted", "written_to_crm"),), _lazy("draft_writer")),
)


def record_scan_failure(cur, company_id, error, category=None):
    """
    One failed scan attempt, in the caller's transaction. Returns "retry" (the company stays
    `checked` and is retried after a growing wait, settings.SCAN_RETRY_BACKOFF_MINUTES) or
    "scan_failed" once it has failed 1 + SCAN_RETRIES times. The failure category (dns, timeout,
    blocked, http_error, other) is recorded either way.
    """
    cur.execute("""UPDATE ae_companies SET scan_attempts = scan_attempts + 1, failure_category = %s,
                   updated_at = NOW() WHERE company_id = %s RETURNING scan_attempts""", (category, company_id))
    row = cur.fetchone()
    if row is None:
        raise status.UnknownCompany(f"company {company_id} is not in ae_companies")
    attempts = row["scan_attempts"]
    if attempts > settings.SCAN_RETRIES:
        status.set_status(cur, company_id, "scan_failed", f"scan failed {attempts} times: {error}")
        return "scan_failed"
    backoff = settings.SCAN_RETRY_BACKOFF_MINUTES
    wait = backoff[min(attempts, len(backoff)) - 1]
    cur.execute("""UPDATE ae_companies SET next_retry_at = NOW() + make_interval(mins => %s),
                   status_reason = %s WHERE company_id = %s""",
                (wait, f"scan attempt {attempts} failed: {error}", company_id))
    return "retry"
