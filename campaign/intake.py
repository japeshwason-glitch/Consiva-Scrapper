"""
Stage 1: intake. Reads PartnerImport companies from the CRM (read-only) and puts them in
ae_companies, with their contacts in ae_contacts.

  * production: every PartnerImport company not yet in ae_companies, as `pending`
    (`held_already_contacted` if the CRM already logged an Email activity for it). A company
    with no usable Website stays pending: stage 3 may still find its domain from its contacts'
    emails, and only then decides `no_website`;
  * legacy: a company that already carries a ReportStatus was processed by the retired
    report script. It is imported once, as `legacy`: no contacts, no stages, no draft, and
    never a CRM write;
  * test cohort (--sample N): N legacy companies are re-opened as `pending` with
    is_test = true, their contacts imported. They run every stage but write nothing to
    the CRM (crm_write refuses them; status.set_status never tries).

Idempotent: ae_companies.company_id and ae_contacts.contact_id are unique and every insert
is ON CONFLICT DO NOTHING, so a second run adds nothing and moves nothing.

The watermark (ae_watermark, job 'intake_prod') is "the highest CRM Id taken in". reconcile()
compares every CRM Id with ae_companies and takes in anything the watermark skipped.
"""
import random
import re
from collections import Counter

import psycopg2.extras

import db

from . import settings, status

WATERMARK_JOB = "intake_prod"
EMAILED = "CRM already has an Email activity for this company"
NEW = "taken in from the CRM"
PROMOTED = "re-opened from legacy as a test company"


def domain_of(website):
    """Bare lowercase host from whatever was typed: scheme, www., port, path, trailing dot."""
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", (website or "").strip().lower())
    host = host.split("/")[0].split("?")[0].split("#")[0].split(":")[0].rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host and not re.search(r"\s", host) else None


def is_business_email(email):
    """A syntactically plain address whose domain is not a free-mail provider."""
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].strip().lower()
    return "." in domain and domain not in settings.FREE_MAIL_DOMAINS


def get_watermark(job_name):
    with db.get_cursor() as cur:
        cur.execute("SELECT last_crm_id FROM ae_watermark WHERE job_name = %s", (job_name,))
        row = cur.fetchone()
    return row["last_crm_id"] if row else 0


def _existing_ids():
    with db.get_cursor() as cur:
        cur.execute("SELECT company_id FROM ae_companies")
        return {r["company_id"] for r in cur.fetchall()}


def _legacy_pool():
    """Company ids that are legacy and not yet test, in id order."""
    with db.get_cursor() as cur:
        cur.execute("SELECT company_id FROM ae_companies WHERE status = 'legacy' AND NOT is_test "
                    "ORDER BY company_id")
        return [r["company_id"] for r in cur.fetchall()]


def pick_sample(n, seed=None, extra=()):
    """n legacy company ids, chosen reproducibly from a seed. `extra` adds ids not yet imported (dry runs)."""
    pool = sorted(set(_legacy_pool()) | set(extra))
    return random.Random(seed).sample(pool, min(n, len(pool)))


def seed_suppression(reader, dry_run=False):
    """Every address with a CRM Bounce event goes into ae_suppression (bounced / crm_history)."""
    bounced = {e.strip().lower() for e in reader.bounced_emails() if e and e.strip()}
    with db.get_cursor() as cur:
        cur.execute("SELECT email FROM ae_suppression")
        new = sorted(bounced - {r["email"] for r in cur.fetchall()})
        if not dry_run:
            psycopg2.extras.execute_values(
                cur, """INSERT INTO ae_suppression (email, reason, source) VALUES %s
                        ON CONFLICT (email) DO NOTHING""",
                [(e, "bounced", "crm_history") for e in new])
    return len(new)


def promote_to_test(cur, company_id):
    """Re-opens one legacy company as a test company. Raises if it is anything else."""
    cur.execute("SELECT status, is_test FROM ae_companies WHERE company_id = %s FOR UPDATE", (company_id,))
    row = cur.fetchone()
    if row is None:
        raise status.UnknownCompany(f"company {company_id} is not in ae_companies")
    if row["status"] != "legacy" or row["is_test"]:
        raise status.InvalidTransition(f"company {company_id} is {row['status']}"
                                       f"{' (test)' if row['is_test'] else ''}, not a legacy company")
    cur.execute("UPDATE ae_companies SET is_test = TRUE WHERE company_id = %s", (company_id,))
    status.set_status(cur, company_id, "pending", PROMOTED)


def _insert_contacts(cur, contacts, counts):
    for ct in contacts:
        cur.execute("""INSERT INTO ae_contacts (contact_id, company_id, email, job_title, first_name, is_primary)
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (contact_id) DO NOTHING RETURNING id""",
                    (ct["contact_id"], ct["company_id"], ct["email"].strip(), ct["job_title"], ct.get("first_name"),
                     ct.get("is_primary")))
        if cur.fetchone():
            counts["contacts_new"] += 1
            counts["contacts_business_email"] += is_business_email(ct["email"])


def _new_counts(cohort):
    return {"cohort": cohort, "companies_new": 0, "legacy_new": 0, "legacy_by_report_status": {},
            "by_status": {}, "contacts_new": 0, "contacts_business_email": 0,
            "held_already_contacted": 0, "suppression_new": 0}


def _run_test_cohort(reader, sample, seed, limit, dry_run, counts):
    extra = ()
    if dry_run:     # nothing was imported, so legacy rows are the CRM's companies with a ReportStatus
        existing = _existing_ids()
        extra = [c["id"] for c in reader.companies_after(0)
                 if c["report_status"] and c["id"] not in existing]
    ids = pick_sample(sample, seed, extra)[: limit or None]
    contacts = reader.contacts(ids) if ids else []
    if dry_run:
        counts["companies_new"] = len(ids)
        counts["by_status"] = {"pending": len(ids)} if ids else {}
        counts["contacts_new"] = len(contacts)
        counts["contacts_business_email"] = sum(is_business_email(c["email"]) for c in contacts)
        return
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for cid in ids:
                promote_to_test(cur, cid)
            _insert_contacts(cur, contacts, counts)
    counts["companies_new"] = len(ids)
    counts["by_status"] = {"pending": len(ids)} if ids else {}


def run_intake(reader, *, sample=None, seed=None, limit=None, batch_size=None, dry_run=False,
               use_watermark=True, exclude_previously_emailed=None, only_ids=None, batch_name=None):
    """
    One intake pass. Returns a counts dict:
      cohort, companies_new, legacy_new, legacy_by_report_status, by_status, contacts_new,
      contacts_business_email, held_already_contacted, suppression_new.
    With dry_run nothing is written to Postgres and the same counts are computed.

    `batch_name` names a held-out batch (settings.BATCHES): the reader must be that batch's reader, every company taken
    in is tagged with it (ae_companies.batch), and it keeps its own watermark. Without it companies are untagged.
    """
    if batch_name is not None and (sample is not None or getattr(reader, "batch", batch_name) != batch_name):
        raise ValueError("a batch intake needs a reader of the same batch and no --sample")
    test = sample is not None
    batch_size = batch_size or settings.INTAKE_BATCH_SIZE
    if exclude_previously_emailed is None:
        exclude_previously_emailed = settings.EXCLUDE_PREVIOUSLY_EMAILED
    counts = _new_counts("test" if test else "prod")
    counts["suppression_new"] = seed_suppression(reader, dry_run)

    if test:
        _run_test_cohort(reader, sample, seed, limit, dry_run, counts)
        return counts

    by_status, legacy_by_rs = Counter(), Counter()
    existing = _existing_ids()
    watermark_job = WATERMARK_JOB if batch_name is None else f"{WATERMARK_JOB}:{batch_name}"
    after = get_watermark(watermark_job) if use_watermark else 0
    if only_ids is not None:
        after = 0              # a selection by id must not depend on, or move, the watermark
    todo = [c for c in reader.companies_after(after) if c["id"] not in existing
            and (only_ids is None or c["id"] in only_ids)]
    if limit:
        todo = todo[:limit]

    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        production = [c for c in batch if not c["report_status"]]
        ids = [c["id"] for c in production]
        contacts = reader.contacts(ids) if ids else []
        emailed = reader.emailed_company_ids(ids) if ids and exclude_previously_emailed else set()

        def plan(c):
            if c["report_status"]:
                return "legacy", f"processed by the previous report run (ReportStatus={c['report_status']})"
            if c["id"] in emailed:
                return "held_already_contacted", EMAILED
            return "pending", NEW

        if dry_run:
            counts.setdefault("would_take_ids", []).extend(c["id"] for c in batch)
            for c in batch:
                by_status[plan(c)[0]] += 1
                if c["report_status"]:
                    legacy_by_rs[c["report_status"]] += 1
            counts["companies_new"] += len(batch)
            counts["contacts_new"] += len(contacts)
            counts["contacts_business_email"] += sum(is_business_email(c["email"]) for c in contacts)
            continue

        with db.get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                new_ids = set()
                for c in batch:
                    first = "legacy" if c["report_status"] else "pending"
                    cur.execute("""INSERT INTO ae_companies (company_id, is_test, domain, status, status_reason,
                                                             website, company_scale, company_name, industry, batch)
                                   VALUES (%s, FALSE, %s, %s, %s, %s, %s, %s, %s, %s)
                                   ON CONFLICT (company_id) DO NOTHING RETURNING company_id""",
                                (c["id"], domain_of(c["website"]), first,
                                 plan(c)[1] if first == "legacy" else NEW,
                                 (c["website"] or None), c.get("company_scale"), c.get("company_name"),
                                 (c.get("industry") or None), batch_name))
                    if cur.fetchone():
                        new_ids.add(c["id"])
                for c in batch:
                    if c["id"] not in new_ids:
                        continue
                    new_status, reason = plan(c)
                    if new_status not in status.INITIAL:
                        status.set_status(cur, c["id"], new_status, reason)
                    by_status[new_status] += 1
                    if c["report_status"]:
                        legacy_by_rs[c["report_status"]] += 1
                _insert_contacts(cur, [ct for ct in contacts if ct["company_id"] in new_ids], counts)
                counts["companies_new"] += len(new_ids)
                if batch and only_ids is None:
                    cur.execute("""INSERT INTO ae_watermark (job_name, last_crm_id) VALUES (%s, %s)
                                   ON CONFLICT (job_name) DO UPDATE
                                   SET last_crm_id = GREATEST(ae_watermark.last_crm_id, EXCLUDED.last_crm_id),
                                       updated_at = NOW()""", (watermark_job, max(c["id"] for c in batch)))

    counts["by_status"] = dict(by_status)
    counts["legacy_new"] = by_status.get("legacy", 0)
    counts["legacy_by_report_status"] = dict(legacy_by_rs)
    counts["held_already_contacted"] = by_status.get("held_already_contacted", 0)
    return counts


def backfill_details(reader):
    """
    Fills, from the CRM, the details drafting needs on rows taken in before they were kept: a contact's
    first name and a company's industry. Only blanks are filled; nothing already there is overwritten.
    Returns {"first_names": n, "industries": n}. Reads the CRM, writes Postgres only.
    """
    own_batch = getattr(reader, "batch", None)          # a reader only backfills the companies of its own batch
    with db.get_cursor() as cur:
        cur.execute("SELECT company_id FROM ae_companies WHERE industry IS NULL AND batch IS NOT DISTINCT FROM %s",
                    (own_batch,))
        no_industry = {r["company_id"] for r in cur.fetchall()}
        cur.execute("""SELECT DISTINCT ct.company_id FROM ae_contacts ct JOIN ae_companies co USING (company_id)
                       WHERE (ct.first_name IS NULL OR ct.is_primary IS NULL) AND co.batch IS NOT DISTINCT FROM %s""",
                    (own_batch,))
        no_name = [r["company_id"] for r in cur.fetchall()]
    done = {"first_names": 0, "industries": 0, "primaries": 0}
    if no_industry:
        with db.get_cursor() as cur:
            for c in reader.companies_after(0):
                if c["id"] in no_industry and (c.get("industry") or "").strip():
                    cur.execute("UPDATE ae_companies SET industry = %s WHERE company_id = %s AND industry IS NULL",
                                (c["industry"].strip(), c["id"]))
                    done["industries"] += cur.rowcount
    if no_name:
        crm_contacts = reader.contacts(no_name)
        with db.get_cursor() as cur:
            for ct in crm_contacts:
                if ct.get("first_name"):
                    cur.execute("UPDATE ae_contacts SET first_name = %s WHERE contact_id = %s AND first_name IS NULL",
                                (ct["first_name"], ct["contact_id"]))
                    done["first_names"] += cur.rowcount
                if ct.get("is_primary") is not None:
                    cur.execute("UPDATE ae_contacts SET is_primary = %s WHERE contact_id = %s AND is_primary IS NULL",
                                (ct["is_primary"], ct["contact_id"]))
                    done["primaries"] += cur.rowcount
    return done


def reconcile(reader, **kw):
    """Compare every CRM Id with ae_companies and take in what the watermark missed."""
    return run_intake(reader, use_watermark=False, **kw)
