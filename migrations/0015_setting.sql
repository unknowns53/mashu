-- Values the user changes from the Settings screen. They live in the store rather than the
-- environment because each agent session's MCP server keeps the environment it started with.
CREATE TABLE setting (
    name TEXT PRIMARY KEY CHECK (name IN ('checkpoint_interval_minutes')),
    value INTEGER NOT NULL CHECK (value BETWEEN 0 AND 1440),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_by TEXT NOT NULL
);
