-- ---------------------------------------------------------------------------
-- 0021: what a night cost, per call, and which reading it was (16.3).
--
-- Two things the ledger could not say, both of which turned into silent losses.
--
-- **Cost was attributed to a run, not to a moment.** extraction_run.input_tokens
-- accumulates across every window, and the daily total summed that column for
-- runs claimed today. A session read in windows across midnight therefore had
-- yesterday's spending counted again as today's, and again the day after: a run
-- that had already spent most of the allowance could never afford its next
-- window, and deferred itself forever while reporting a budget that was full.
-- The run total stays, because a run's own cost is worth showing. What the day
-- is summed from is now one row per model call, stamped when the call happened.
--
-- **A run could not say how it had been reading.** Windows taken from the front
-- cover every turn up to the mark, so a repeat after it may be folded to a
-- place-holder — the body really was sent. A reading that selects turns around
-- what a session flagged covers nothing in particular, and folding against the
-- file would leave a place-holder standing for a body no call ever carried.
-- Which of the two ran is therefore not a statistic; it decides what the next
-- window is allowed to assume, and it has to survive between passes.
-- ---------------------------------------------------------------------------

CREATE TABLE extraction_charge (
    charge_id     BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES extraction_run(run_id) ON DELETE CASCADE,
    charged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER
);

CREATE INDEX idx_charge_day ON extraction_charge (charged_at);
CREATE INDEX idx_charge_run ON extraction_charge (run_id);

-- What has already been spent, so the switch does not hand today a clean slate
-- it did not earn. One row per run is the best that can be reconstructed; from
-- here on it is one per call.
INSERT INTO extraction_charge (run_id, charged_at, input_tokens, output_tokens)
SELECT run_id, coalesce(completed_at, claimed_at, created_at), input_tokens, output_tokens
FROM extraction_run
WHERE coalesce(input_tokens, 0) > 0;

ALTER TABLE extraction_run ADD COLUMN reading TEXT;
