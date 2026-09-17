-- Store agent close proposals; only task.close closes a task (docs/mashu-v3.md 6).
CREATE TABLE task_close_proposal (
    task_id     UUID PRIMARY KEY REFERENCES task(task_id),
    outcome     TEXT NOT NULL CHECK (outcome IN ('completed', 'abandoned', 'superseded')),
    -- A proposal must include grounds for the reviewer.
    reason      TEXT NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    -- State version used by the proposal; later updates make it stale.
    state_at    TIMESTAMPTZ NOT NULL,
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    proposed_by TEXT NOT NULL
);

-- Proposals are replaced in place; event_log retains proposal history.
