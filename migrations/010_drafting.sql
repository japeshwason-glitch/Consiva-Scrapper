-- 010_drafting.sql -- Auto Email Campaign, milestone 3: drafting (stage 9), NeverBounce safety, title providers
--
-- Nothing here touches the CRM or any pre-existing pipeline table.

-- A. Where a NeverBounce verdict came from. NULL means unknown, which is never accepted for production.
ALTER TABLE ae_contacts ADD COLUMN nb_source TEXT CHECK (nb_source IN ('mock', 'live'));
ALTER TABLE ae_verification_cache ADD COLUMN source TEXT CHECK (source IN ('mock', 'live'));

-- B. The title classifier can now be OpenAI or the gignati server; each model's answers are cached on their own.
ALTER TABLE ae_title_cache ADD COLUMN provider TEXT, ADD COLUMN model TEXT;
ALTER TABLE ae_title_cache DROP CONSTRAINT ae_title_cache_normalized_title_key;
CREATE UNIQUE INDEX ae_title_cache_key
    ON ae_title_cache (normalized_title, source, COALESCE(provider, ''), COALESCE(model, ''));

-- C. Drafting. The model never sees a contact's name; code inserts first_name after generation.
ALTER TABLE ae_contacts  ADD COLUMN first_name TEXT;
ALTER TABLE ae_companies ADD COLUMN industry TEXT;          -- the CRM Industry, "what the company does"
ALTER TABLE ae_drafts
    ADD COLUMN plain_text         TEXT,
    ADD COLUMN opening_line       TEXT,
    ADD COLUMN finding_sentence   TEXT,
    ADD COLUMN contact_function   TEXT,                     -- the only thing about the contact the model is told
    ADD COLUMN findings           JSONB,                    -- the structured findings the slots were built from
    ADD COLUMN provider           TEXT,
    ADD COLUMN model              TEXT,
    ADD COLUMN slot_source        JSONB,                    -- {"opening_line": "model|retry|fallback", "finding_sentence": ...}
    ADD COLUMN validation_log     JSONB,                    -- rule failures seen on each attempt
    ADD COLUMN attempts           INTEGER,
    ADD COLUMN input_tokens       INTEGER,
    ADD COLUMN output_tokens      INTEGER;
