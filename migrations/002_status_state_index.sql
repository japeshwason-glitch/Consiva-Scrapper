-- run_daily screens new companies one state at a time, so every cycle issues
-- six queries shaped:
--
--   SELECT * FROM companies
--    WHERE status = 'discovered' AND state_code = ?
--    ORDER BY updated_at LIMIT ?
--
-- With only idx_companies_status to work from, Postgres chose a parallel
-- sequential scan of all 1.4M rows and a top-N sort: 177ms each, six per
-- cycle, every cycle of every night.
--
-- The composite index answers the filter directly, and carrying updated_at as
-- the third column lets the same index supply the ordering, so the sort
-- disappears too.
CREATE INDEX IF NOT EXISTS idx_companies_status_state
    ON companies (status, state_code, updated_at);

-- crm_push and stage 07 both filter on delivered_at IS NULL among exported
-- rows. Partial, because the rows that matter are always the undelivered ones
-- and the index then stays small however many leads have shipped.
CREATE INDEX IF NOT EXISTS idx_companies_undelivered
    ON companies (status, delivered_at)
    WHERE delivered_at IS NULL;
