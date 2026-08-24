-- ---------------------------------------------------------------------------
-- 0019: accidents, counted rather than felt (27.5, 30 段 D, 反証条件 3).
--
-- 27.5 refuses to judge the switchover by whether it "seems to be running":
-- an impression cannot say whether the move succeeded, so the trial counts
-- occurrences and splits them by cause. The split is the point. Each cause
-- leads somewhere different, and a tally without one says only that something
-- is wrong.
--
-- Two kinds, because the two ways this system can fail an agent run opposite
-- ways and a single list would blur them.
--
-- missed: the store held it as active and the agent worked without it (27.5).
--   bootstrap  it should have been pushed and was not (21.2)
--   pull       the index was there and the agent did not go and look (6.1)
--   capture    it was never written down at all — the cause 段 E adds, and the
--              one the old plan could not express, because before unattended
--              capture there was nobody to have missed it
--
-- stale: something withdrawn or expired came back as current (反証条件 3).
--   filter     a temporary context outlived its window (25.2) — an outright bug
--   inventory  a rule with a shelf life was written on the indefinite side and
--              the sweep for those (段 C) did not catch it
--   marker     the user said it was finished and nothing picked that up, which
--              is a recall problem in the marker path rather than in retrieval
--
-- note is NOT NULL for the reason 27.5 gives. A row that records an accident
-- without saying what happened cannot be sorted into a cause later, and the
-- causes are what the count is for.
--
-- occurred_at is separate from created_at: an accident is usually noticed
-- after the fact, and during a week nobody attends, well after.
-- ---------------------------------------------------------------------------

CREATE TABLE incident (
    incident_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT NOT NULL CHECK (kind IN ('missed', 'stale')),
    cause       TEXT NOT NULL,
    memory_id   UUID REFERENCES memory_entity (memory_id),
    scope_id    UUID REFERENCES scope (scope_id),
    note        TEXT NOT NULL CHECK (length(trim(note)) > 0),
    recorded_by TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'missed' AND cause IN ('bootstrap', 'pull', 'capture'))
        OR (kind = 'stale' AND cause IN ('filter', 'inventory', 'marker'))
    )
);

CREATE INDEX idx_incident_occurred ON incident (occurred_at DESC);
CREATE INDEX idx_incident_kind ON incident (kind, cause);
