-- consent_gap_score changes polarity: 100 now means CLEAN, 0 means worst.
--
-- fingerprints.consent_score() produces the new scale from here on, so rows
-- already stored on the old one have to be flipped or the two coexist and
-- every filter reads half the table backwards.
--
-- This lives in a migration, not a script, precisely because "100 - score" is
-- correct exactly once. migrate.py records and checksums each file, so it
-- cannot run twice -- which a hand-run script could, silently restoring the
-- old scale with nothing in the data to show which way round it is.
--
-- Rows where the score is NULL are untouched: unmeasured must not become a
-- perfect 100.
UPDATE companies
   SET consent_gap_score = 100 - consent_gap_score
 WHERE consent_gap_score IS NOT NULL;
