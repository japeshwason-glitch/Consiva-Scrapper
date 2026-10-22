-- 013_express.sql -- Auto Email Campaign: express batches (no contact ranking, no NeverBounce)
--
-- A held-out batch listed in AE_EXPRESS_BATCHES (default goodfirms, clutch, gf_clutch) skips contact ranking and
-- NeverBounce: stage 7 takes the CRM's primary contact, stage 8 records that the address was NOT verified, and stage 9
-- drafts it. Both need a place to remember it:
--   * ae_contacts.is_primary  the CRM contact's IsPrimary flag, read at intake (NULL = not read yet);
--   * nb_source 'skipped'     "NeverBounce was not asked", allowed by drafting for an express batch only.
-- Nothing here touches the CRM.
ALTER TABLE ae_contacts ADD COLUMN is_primary BOOLEAN;
ALTER TABLE ae_contacts DROP CONSTRAINT IF EXISTS ae_contacts_nb_source_check;
ALTER TABLE ae_contacts ADD CONSTRAINT ae_contacts_nb_source_check CHECK (nb_source IN ('mock', 'live', 'skipped'));
