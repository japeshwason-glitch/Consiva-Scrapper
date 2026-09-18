-- Reverts migration 003. consent_gap_score goes back to 100 = WORST, 0 = clean.
--
-- 003 inverted it so 100 read as clean. On review the original direction was
-- kept: the column measures the SIZE of a gap, which is what it is called, so
-- a high number meaning a big gap is what a reader expects.
--
-- Same reasoning as 003 for why this is a migration and not a script --
-- "100 - score" is correct exactly once, and migrate.py records and checksums
-- each file so it cannot run twice.
--
-- NULL is left alone: unmeasured must not become a perfect 0.
UPDATE companies
   SET consent_gap_score = 100 - consent_gap_score
 WHERE consent_gap_score IS NOT NULL;
