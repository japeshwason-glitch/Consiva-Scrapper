"""
Stage 9: drafting. For each `verified` company, one first-email draft written to ae_drafts (Postgres only:
nothing in this stage touches the CRM, in any cohort).

Input gate (a company that fails stays `verified` and is counted, so it can be retried):
  * the chosen contact is OK or RISKY, passed the free checks, and is not in ae_suppression now
    (suppressed since verification -> the company becomes `suppressed`);
  * in production the contact's verdict came from a LIVE NeverBounce call. A mock (or unknown) verdict is
    never drafted for a production company: the check is in draft_one() itself, not only in the loop;
  * a report was generated (report_url) and is at most settings.REPORT_FRESH_DAYS old;
  * the daily cap (settings.DRAFTS_PER_DAY, IST calendar day) is not used up.

The email is a band template (fixed wording) with three sentences from the model, validated in code:
a bad answer is regenerated once, and a slot still bad after that uses the plain fallback. Each slot's
source (model / retry / fallback), the validation failures and the token usage are stored on the draft.
If the model cannot be reached the company stays `verified` and the stage stops (a down server is not
hammered, and a missing key is reported, never drafted around).
"""
import json
from collections import Counter

import psycopg2.extras

import db

from . import draft_findings, draft_model, draft_templates, draft_validate, settings, status
from . import settings_drafts as cfg

SLOTS = draft_validate.SLOTS


class MockVerdictRefused(Exception):
    """A production company's chosen contact was verified by a mock (or unknown) NeverBounce verdict."""


class DraftBlocked(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def drafts_today(cur):
    cur.execute("""SELECT COUNT(*) n FROM ae_drafts
                   WHERE (generated_at AT TIME ZONE 'Asia/Kolkata')::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date""")
    return cur.fetchone()["n"]


def _contact(cur, company):
    cur.execute("SELECT * FROM ae_contacts WHERE contact_id = %s", (company["chosen_contact_id"],))
    return cur.fetchone()


def gate_reason(cur, company, contact):
    """Why this company cannot be drafted right now, or None. 'suppressed' means move it to `suppressed`."""
    if contact is None or not contact["check_passed"]:
        return "no_contact"
    if not company["is_test"] and contact["nb_source"] != "live":
        return "mock_verdict"
    if contact["nb_decision"] not in ("OK", "RISKY"):
        return "contact_not_verified"
    cur.execute("SELECT 1 FROM ae_suppression WHERE email = LOWER(%s)", (contact["email"],))
    if cur.fetchone():
        return "suppressed"
    if not company["report_url"]:
        return "no_report"
    cur.execute("""SELECT report_date IS NOT NULL AND report_date >= NOW() - make_interval(days => %s) AS fresh
                   FROM ae_companies WHERE company_id = %s""", (settings.REPORT_FRESH_DAYS, company["company_id"]))
    if not cur.fetchone()["fresh"]:
        return "report_stale"
    return None


def _generate_slots(payload, findings, generator):
    """
    (slots, sources, log, tokens, attempts). Attempt 1; if any slot fails, one regeneration; slots still
    failing take the plain fallback. A slot that passed on the first attempt keeps that text.
    """
    slots, sources, log = {}, {}, []
    tokens = {"input_tokens": 0, "output_tokens": 0}
    attempts = 0
    for label in ("model", "retry"):
        pending = [s for s in SLOTS if s not in slots]
        if not pending:
            break
        attempts += 1
        raw, used = generator(payload)
        tokens["input_tokens"] += used["input_tokens"]
        tokens["output_tokens"] += used["output_tokens"]
        failed = ({s: ["bad_json"] for s in SLOTS} if raw is None else draft_validate.validate(raw, findings))
        log.append({s: f for s, f in failed.items() if s in pending})
        for s in pending:
            if s not in failed:
                slots[s], sources[s] = raw[s].strip(), label
    if len(slots) < len(SLOTS):
        fallback = draft_templates.fallback_slots(findings)
        for s in SLOTS:
            if s not in slots:
                slots[s], sources[s] = fallback[s], "fallback"
    return slots, sources, log, tokens, attempts


def draft_one(cur, company, generator):
    """
    Builds and stores one company's draft and moves it to `drafted`, in the caller's transaction.
    Refuses (MockVerdictRefused) a production company whose chosen contact's verdict is not LIVE.
    """
    contact = _contact(cur, company)
    if not company["is_test"] and (contact is None or contact["nb_source"] != "live"):
        raise MockVerdictRefused(f"company {company['company_id']}: the chosen contact's NeverBounce verdict "
                                 f"is not from a live call; a production company is never drafted on it")
    reason = gate_reason(cur, company, contact)
    if reason:
        raise DraftBlocked(reason)
    cur.execute("SELECT * FROM report_scans WHERE id = %s", (company["scan_id"],))
    scan = cur.fetchone()
    findings = draft_findings.build_findings(scan, {
        "company_name": company["company_name"] or company["domain"], "domain": company["domain"],
        "industry": company["industry"], "band": company["band"], "score": company["score"]})
    if cfg.USE_MODEL:
        payload = draft_model.build_payload(findings, contact["title_function"])
        slots, sources, log, tokens, attempts = _generate_slots(payload, findings, generator)
        provider, model, prompt_version = cfg.PROVIDER, cfg.model_for(cfg.PROVIDER), cfg.PROMPT_VERSION
    else:                                    # the approved fixed template: nothing is generated
        slots, sources, log, attempts = {s: "" for s in SLOTS}, {s: "fixed" for s in SLOTS}, [], 0
        tokens = {"input_tokens": 0, "output_tokens": 0}
        provider, model, prompt_version = "fixed", "none", cfg.FIXED_PROMPT_VERSION
    facts = {"company": findings["company_name"], "n_tracking": findings["tracking_cookie_count"],
             "no_banner": not findings["cookie_banner"], "n_third": findings["third_party_count"]}
    mail = draft_templates.render(company["band"], slots["opening_line"], slots["finding_sentence"],
                                  slots["impact_sentence"], contact["first_name"], company["report_url"],
                                  company["domain"], facts)
    cur.execute("""INSERT INTO ae_drafts (company_id, contact_id, to_email, subject, body_html, plain_text, band,
                       draft_status, template_version, prompt_version, opening_line, finding_sentence,
                       contact_function, findings, provider, model, slot_source, validation_log, attempts,
                       input_tokens, output_tokens)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (company_id) DO UPDATE SET
                       contact_id = EXCLUDED.contact_id, to_email = EXCLUDED.to_email, subject = EXCLUDED.subject,
                       body_html = EXCLUDED.body_html, plain_text = EXCLUDED.plain_text, band = EXCLUDED.band,
                       draft_status = EXCLUDED.draft_status, template_version = EXCLUDED.template_version,
                       prompt_version = EXCLUDED.prompt_version, opening_line = EXCLUDED.opening_line,
                       finding_sentence = EXCLUDED.finding_sentence, contact_function = EXCLUDED.contact_function,
                       findings = EXCLUDED.findings, provider = EXCLUDED.provider, model = EXCLUDED.model,
                       slot_source = EXCLUDED.slot_source, validation_log = EXCLUDED.validation_log,
                       attempts = EXCLUDED.attempts, input_tokens = EXCLUDED.input_tokens,
                       output_tokens = EXCLUDED.output_tokens, generated_at = NOW(),
                       written_to_crm_at = NULL, sent_at = NULL""",
                (company["company_id"], contact["contact_id"], contact["email"], mail.subject, mail.html, mail.text,
                 company["band"], "ready" if contact["nb_decision"] == "OK" else "risky", mail.template_version,
                 prompt_version, slots["opening_line"], slots["finding_sentence"], contact["title_function"],
                 json.dumps(findings), provider, model, json.dumps(sources), json.dumps(log),
                 attempts, tokens["input_tokens"], tokens["output_tokens"]))
    status.set_status(cur, company["company_id"], "drafted",
                      f"{'ready' if contact['nb_decision'] == 'OK' else 'risky'} draft, band {company['band']}, "
                      f"slots {','.join(f'{s}={sources[s]}' for s in SLOTS)}")
    return {"company_id": company["company_id"], "slot_source": sources, "validation_failures": log,
            "attempts": attempts, **tokens}


def run(limit=None, dry_run=False, only_test=None, generator=None):
    """Counts: drafted, blocked{reason: n}, suppressed, slot_sources, tokens, cost estimate, per_draft, stopped, error."""
    generator = generator or draft_model.generate
    counts = {"claimed": 0, "drafted": 0, "would_draft": 0, "suppressed": 0, "blocked": {}, "errors": 0,
              "error_detail": [], "slot_sources": {}, "input_tokens": 0, "output_tokens": 0, "per_draft": [],
              "stopped": None}
    sources = Counter()
    blocked = Counter()
    with status.claim("verified", settings.DRAFTS_PER_DAY * 4, is_test=only_test) as (cur, rows):
        left = settings.DRAFTS_PER_DAY - drafts_today(cur)
        if limit:
            left = min(left, limit)
        counts["claimed"] = len(rows)
        for company in rows:
            if left <= 0:
                counts["stopped"] = counts["stopped"] or "daily_cap"
                break
            contact = _contact(cur, company)
            reason = gate_reason(cur, company, contact)
            if reason == "suppressed":
                if not dry_run:
                    status.set_status(cur, company["company_id"], "suppressed",
                                      "the chosen contact was suppressed after verification")
                counts["suppressed"] += 1
                continue
            if reason:
                blocked[reason] += 1
                continue
            if dry_run:
                counts["would_draft"] += 1
                left -= 1
                continue
            try:
                with status.step(cur):
                    out = draft_one(cur, company, generator)
            except draft_model.ModelUnavailable as exc:
                counts["stopped"], counts["error"] = "model_unavailable", str(exc)
                break
            except Exception as exc:
                counts["errors"] += 1
                counts["error_detail"].append(f"{company['company_id']}: {type(exc).__name__}: {str(exc)[:160]}")
                continue
            counts["drafted"] += 1
            left -= 1
            counts["input_tokens"] += out["input_tokens"]
            counts["output_tokens"] += out["output_tokens"]
            sources.update(out["slot_source"].values())
            counts["per_draft"].append(out)
    counts["blocked"], counts["slot_sources"] = dict(blocked), dict(sources)
    if counts["drafted"]:
        n = counts["drafted"]
        counts["avg_input_tokens"] = round(counts["input_tokens"] / n, 1)
        counts["avg_output_tokens"] = round(counts["output_tokens"] / n, 1)
        counts["est_cost_per_1000_usd"] = round(draft_model.estimate_cost(counts["input_tokens"], counts["output_tokens"]) / n * 1000, 4)
        counts["cost_basis"] = draft_model.estimate_cost_note()
    return counts
