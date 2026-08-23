-- ---------------------------------------------------------------------------
-- 0014: what the worker needs before it may file anything (16.3, 30 段 A).
--
-- scope_route is the explicit map from a working directory to a scope. The
-- specification is deliberate that the worker does not guess: a wrong scope is
-- not a smaller version of the right one, it puts knowledge where the sessions
-- that need it will never look, and nothing about the result reads as wrong.
-- An unmapped directory is held on the ledger and reported instead.
--
-- checkpoint is what keeps the cost of capture bounded. Extraction reads the
-- scratch and whatever arrived after the last successful run, not the whole
-- transcript again; without a mark the second run of a long session pays for
-- the first one over again, every night.
-- ---------------------------------------------------------------------------

CREATE TABLE scope_route (
    route_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path_prefix TEXT NOT NULL UNIQUE,
    scope_id    UUID NOT NULL REFERENCES scope(scope_id),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Held on the run, not on agent_session: a transcript from a CLI that never
-- called the MCP server has no session row, and those are exactly the ones
-- automatic capture exists to reach.
-- created_at is now(), which is transaction start time, so a burst enqueued in
-- one transaction carries one identical timestamp and the queue order falls to
-- whatever the scan returns. proposal already carries a sequence for the same
-- reason; the ledger needs one too, or "oldest first" is not a claim about
-- anything.
ALTER TABLE extraction_run ADD COLUMN seq             BIGSERIAL;
ALTER TABLE extraction_run ADD COLUMN cwd             TEXT;
ALTER TABLE extraction_run ADD COLUMN checkpoint      INT;
ALTER TABLE extraction_run ADD COLUMN transcript_path TEXT;

-- held is what an unmapped working directory produces. Not skipped: skipped
-- means dealt with, and this one becomes workable the moment somebody adds the
-- route, so it has to stay distinguishable and be releasable back into the
-- queue. Guessing the scope instead is the one thing 16.3 rules out.
ALTER TABLE extraction_run DROP CONSTRAINT extraction_run_state_check;
ALTER TABLE extraction_run ADD CONSTRAINT extraction_run_state_check
    CHECK (state IN ('queued', 'running', 'succeeded', 'skipped', 'held',
                     'retrying', 'failed'));

CREATE INDEX idx_run_checkpoint ON extraction_run (source_cli, external_session_id)
    WHERE state = 'succeeded';
