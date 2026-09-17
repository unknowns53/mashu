-- What an agent may say about a task having ended (docs/mashu-v3.md 6).
-- Nothing here writes task.status: a row is answered by a close, never the
-- close itself.
CREATE TABLE task_close_proposal (
    task_id     UUID PRIMARY KEY REFERENCES task(task_id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('completed', 'abandoned', 'superseded')),
    -- NOT NULL, unlike task.close_reason: a person closing a task was there,
    -- and a person reading a proposal was not.
    reason      TEXT NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    -- The task_state.updated_at proposed against, so work written afterwards
    -- reads as having overtaken this.
    state_at    TIMESTAMPTZ NOT NULL,
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    proposed_by TEXT NOT NULL
);

-- Replaced in place like task_state, so no append-only trigger. Who proposed
-- what and when is in event_log.
