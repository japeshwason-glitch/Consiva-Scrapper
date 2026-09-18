-- Final direction for consent_gap_score: 0 = MOST SEVERE gap, 100 = perfectly
-- aligned with DPDP and not a lead.
--
-- Read it as alignment, not as a quantity of breach. 100 means nothing to sell
-- -- either no gap at all, or the company already runs a consent platform.
--
-- History, so the next reader is not confused by three migrations touching one
-- column: 003 inverted it to this direction, 004 put it back, and 005 returns
-- to it. The direction is settled here; fingerprints.ALIGNED is the single
-- place the code defines it.
--
-- A migration rather than a script, because "100 - score" is correct exactly
-- once and migrate.py checksums each file so it cannot run twice. NULL is left
-- alone: unmeasured must not become a perfect 100.
UPDATE companies
   SET consent_gap_score = 100 - consent_gap_score
 WHERE consent_gap_score IS NOT NULL;
