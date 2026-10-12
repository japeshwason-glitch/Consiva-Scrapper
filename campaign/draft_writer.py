"""
Stage 10: write a company's draft into the AE_ columns of its CRM row, so the CRM team can review and send
it. Production companies only (crm_write refuses test and legacy companies); a `drafted` company whose draft
is ready or risky moves to `written_to_crm` in the same step as the write.

Written: AE_DraftBand, AE_DraftBody (HTML), AE_DraftEmail, AE_DraftStatus (ready / risky), AE_DraftSubject,
AE_DraftVersion (the template label, <= 20 characters), AE_DraftGeneratedAt (server time).
Not written: AE_DraftContactId (an int in the CRM; our contact ids are GUIDs, so there is nothing valid to put
there) and AE_DraftSentAt (nothing has been sent; the CRM team sets it).
Template wording is still a PLACEHOLDER: AE_DraftVersion carries that tag, and while it does the draft is NOT
written unless settings.ALLOW_PLACEHOLDER_AE_WRITE (demo only), in which case AE_DraftStatus is 'expired', never
ready or risky. crm_write enforces the same rule on its own.
"""
import psycopg2.extras

from . import crm_write, draft_templates, settings, status


def is_placeholder(draft):
    return "placeholder" in (draft["template_version"] or "").lower()


def columns_for(draft):
    # A placeholder draft is shown to the CRM team but can never be sent by mistake: it goes in as 'expired'.
    crm_status = "expired" if is_placeholder(draft) else draft["draft_status"]
    return {"AE_DraftBand": draft["band"], "AE_DraftBody": draft["body_html"], "AE_DraftEmail": draft["to_email"],
            "AE_DraftStatus": crm_status, "AE_DraftSubject": draft["subject"],
            "AE_DraftVersion": draft_templates.crm_version(draft["template_version"]),
            "AE_DraftGeneratedAt": crm_write.SERVER_NOW}


def run(limit=None, dry_run=False, only_test=False):
    """Counts: claimed, written, blocked, errors. Test companies are never written (only_test is ignored for them)."""
    counts = {"claimed": 0, "written": 0, "would_write": 0, "blocked": {}, "errors": 0, "error_detail": []}
    with status.claim("drafted", limit or 100, is_test=False) as (cur, rows):
        counts["claimed"] = len(rows)
        for company in rows:
            cur.execute("SELECT * FROM ae_drafts WHERE company_id = %s", (company["company_id"],))
            draft = cur.fetchone()
            if draft is None or draft["draft_status"] not in ("ready", "risky"):
                counts["blocked"]["no_ready_draft"] = counts["blocked"].get("no_ready_draft", 0) + 1
                continue
            if is_placeholder(draft) and not settings.ALLOW_PLACEHOLDER_AE_WRITE:
                counts["blocked"]["placeholder_template"] = counts["blocked"].get("placeholder_template", 0) + 1
                continue
            if dry_run:
                counts["would_write"] += 1
                continue
            try:
                with status.step(cur):
                    status.set_status(cur, company["company_id"], "written_to_crm",
                                      f"draft written to the CRM AE_ columns ({draft['draft_status']})",
                                      crm=columns_for(draft))
                    cur.execute("UPDATE ae_drafts SET written_to_crm_at = NOW() WHERE company_id = %s",
                                (company["company_id"],))
                counts["written"] += 1
            except Exception as exc:
                counts["errors"] += 1
                counts["error_detail"].append(f"{company['company_id']}: {type(exc).__name__}: {str(exc)[:160]}")
    return counts
