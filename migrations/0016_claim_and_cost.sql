-- ---------------------------------------------------------------------------
-- 0016: telling a working run from an abandoned one, and paying for what was
-- actually read (16.3).
--
-- Stale-run recovery was keyed on created_at, which is when the transcript was
-- enqueued, not when a worker took it. Every run older than the threshold at
-- the moment it was claimed — a sweeper backlog, and by construction every run
-- the daily budget deferred — was immediately re-claimable by a second worker
-- while the first was still inside its model call. Both would then read from
-- the same checkpoint and both would file. The row lock does not reach across
-- that boundary: the claim commits before the work starts, which is what lets
-- the work take ten minutes without holding a transaction open.
--
-- dropped holds what the extraction looked at and chose not to propose. The
-- plan's first falsification condition says a miss is a loss that can still be
-- observed because it stays in the scratch; the scratch is cleared on success,
-- so without this that claim was not true of the implementation.
-- ---------------------------------------------------------------------------

ALTER TABLE extraction_run ADD COLUMN claimed_at TIMESTAMPTZ;
ALTER TABLE extraction_run ADD COLUMN dropped    JSONB;

-- Anything already running was claimed at some unknown past moment. Treating
-- it as claimed now is the safe reading: it delays reclaiming a genuinely dead
-- worker by one threshold, where the opposite error runs two extractions over
-- the same transcript.
UPDATE extraction_run SET claimed_at = now() WHERE state = 'running';

CREATE INDEX idx_run_claimed ON extraction_run (claimed_at) WHERE state = 'running';

-- The old index could only serve checkpoint_for, and that query has to see
-- runs that are still going as well as ones that finished, so a predicate
-- limited to succeeded made it unusable.
DROP INDEX IF EXISTS idx_run_checkpoint;
CREATE INDEX idx_run_checkpoint ON extraction_run (source_cli, external_session_id, checkpoint);
