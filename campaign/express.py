"""
Express batches: stages 7 and 8 without contact ranking and without NeverBounce.

For a held-out batch listed in settings.EXPRESS_BATCHES (the GoodFirms and Clutch companies), whose contacts are
addresses published on the company's own website and have no job titles:

  stage 7 (rank)    `reported` -> `contact_ranked`: the CRM's primary contact (IsPrimary) of the contacts that passed
                    the free checks; if none is flagged, the first. No title classification, no model call. A company
                    whose contacts all failed the free checks is `held_no_contact` (stage 2 already makes that rare).
  stage 8 (verify)  `contact_ranked` -> `verified`: NeverBounce is NOT asked. The contact is recorded as
                    nb_verdict 'not_verified', nb_source 'skipped', nb_decision 'OK', so the draft is `ready`; a contact
                    that has since been suppressed makes the company `suppressed`. No credit is used.

Only a run inside an express batch (status.in_batch) gets here: ranking.run and verify.run hand over to this module
when express.active(); any other run, the scheduled jobs included, keeps the normal stages. drafting.py accepts
nb_source 'skipped' for a company whose batch is an express batch, and for no one else.
"""
from . import settings, status


def active():
    """True while a run is inside an express batch."""
    return status._BATCH is not None and status._BATCH in settings.EXPRESS_BATCHES


def is_express(company):
    return company.get("batch") is not None and company["batch"] in settings.EXPRESS_BATCHES


def _new_counts(**extra):
    return {"express": True, "companies_claimed": 0, "errors": 0, "error_detail": [], **extra}


def _passing_contacts(cur, company_id):
    cur.execute("""SELECT * FROM ae_contacts WHERE company_id = %s AND check_passed
                   ORDER BY COALESCE(is_primary, FALSE) DESC, id""", (company_id,))
    return cur.fetchall()


def rank(limit=None, dry_run=False, only_test=None, **_):
    counts = _new_counts(contact_ranked=0, held_no_contact=0)
    with status.claim("reported", limit, is_test=only_test) as (cur, companies):
        for co in companies:
            if co["is_test"] or not is_express(co):
                continue                                # the scope already ensures this; belt and braces
            counts["companies_claimed"] += 1
            contacts = _passing_contacts(cur, co["company_id"])
            outcome = "contact_ranked" if contacts else "held_no_contact"
            counts[outcome] += 1
            if dry_run:
                continue
            try:
                with status.step(cur):
                    if contacts:
                        cur.execute("UPDATE ae_contacts SET rank = NULL, rank_reason = NULL WHERE company_id = %s",
                                    (co["company_id"],))
                        for i, ct in enumerate(contacts, 1):
                            cur.execute("UPDATE ae_contacts SET rank = %s, rank_reason = %s WHERE id = %s",
                                        (i, "express batch: the CRM's primary contact" if i == 1
                                         else "express batch: not the primary contact", ct["id"]))
                        status.set_status(cur, co["company_id"], "contact_ranked",
                                          f"express batch: primary contact {contacts[0]['contact_id']}, no title ranking")
                    else:
                        status.set_status(cur, co["company_id"], "held_no_contact",
                                          "express batch: no contact passed the free checks")
            except Exception as exc:                    # one company must not stop the batch
                counts[outcome] -= 1
                counts["errors"] += 1
                counts["error_detail"].append(f"{co['company_id']}: {type(exc).__name__}: {str(exc)[:160]}")
    return counts


def verify(limit=None, dry_run=False, only_test=None, **_):
    counts = _new_counts(verified=0, suppressed=0, api_calls=0, credits_used=0)
    with status.claim("contact_ranked", limit, is_test=only_test) as (cur, companies):
        for co in companies:
            if co["is_test"] or not is_express(co):
                continue
            counts["companies_claimed"] += 1
            cur.execute("SELECT * FROM ae_contacts WHERE company_id = %s AND rank = 1 AND check_passed", (co["company_id"],))
            chosen = cur.fetchone()
            suppressed = False
            if chosen:
                cur.execute("SELECT 1 FROM ae_suppression WHERE email = LOWER(%s)", (chosen["email"],))
                suppressed = cur.fetchone() is not None
            outcome = "verified" if chosen and not suppressed else "suppressed"
            counts[outcome] += 1
            if dry_run:
                continue
            try:
                with status.step(cur):
                    if outcome == "verified":
                        cur.execute("""UPDATE ae_contacts SET nb_verdict = 'not_verified', nb_decision = 'OK',
                                       nb_source = 'skipped', nb_checked_at = NOW() WHERE id = %s""", (chosen["id"],))
                        cur.execute("UPDATE ae_companies SET chosen_contact_id = %s, risky = FALSE WHERE company_id = %s",
                                    (chosen["contact_id"], co["company_id"]))
                        status.set_status(cur, co["company_id"], "verified",
                                          f"express batch: NeverBounce skipped; contact {chosen['contact_id']} not verified")
                    else:
                        status.set_status(cur, co["company_id"], "suppressed",
                                          "express batch: the chosen contact is suppressed" if chosen
                                          else "express batch: no ranked contact")
            except Exception as exc:
                counts[outcome] -= 1
                counts["errors"] += 1
                counts["error_detail"].append(f"{co['company_id']}: {type(exc).__name__}: {str(exc)[:160]}")
    return counts
