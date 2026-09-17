-- Project State schema (docs/mashu-v3.md, sections 5, 7, 14).

CREATE TABLE project (
    project_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    scope_id    UUID REFERENCES scope(scope_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);

-- A task groups a unit of work (5.2).
CREATE TABLE task (
    task_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       UUID NOT NULL REFERENCES project(project_id),
    name             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    outcome          TEXT CHECK (outcome IN ('completed', 'abandoned', 'superseded')),
    close_reason     TEXT,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    active_until     TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT NOT NULL,
    closed_at        TIMESTAMPTZ,
    CHECK ((status = 'closed') = (outcome IS NOT NULL)),
    CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
    CHECK (status = 'closed' OR close_reason IS NULL)
);
CREATE INDEX task_project ON task (project_id);
CREATE INDEX task_name_trgm ON task USING gin (name gin_trgm_ops);

-- Check list length and per-item length in one expression.
CREATE FUNCTION bounded_list(items TEXT[], max_items INT, max_chars INT)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(cardinality(items), 0) <= max_items
       AND coalesce((SELECT max(char_length(item)) FROM unnest(items) AS item), 0) <= max_chars
$$;

-- Current task state and its optimistic-lock timestamp (5.3).
CREATE TABLE task_state (
    task_id        UUID PRIMARY KEY REFERENCES task(task_id),
    goal           TEXT CHECK (char_length(goal) <= 300),
    approach       TEXT CHECK (char_length(approach) <= 500),
    status_text    TEXT CHECK (char_length(status_text) <= 500),
    open_questions TEXT[] NOT NULL DEFAULT '{}' CHECK (bounded_list(open_questions, 5, 300)),
    blockers       TEXT[] NOT NULL DEFAULT '{}' CHECK (bounded_list(blockers, 5, 300)),
    next_actions   TEXT[] NOT NULL DEFAULT '{}' CHECK (bounded_list(next_actions, 5, 300)),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_by     TEXT NOT NULL
);
CREATE INDEX task_state_goal_trgm ON task_state USING gin (goal gin_trgm_ops);
CREATE INDEX task_state_status_trgm ON task_state USING gin (status_text gin_trgm_ops);

-- Freeze the replaced state as a checkpoint (5.6).
CREATE TABLE task_checkpoint (
    checkpoint_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id        UUID NOT NULL REFERENCES task(task_id),
    what_changed   TEXT NOT NULL CHECK (char_length(what_changed) <= 500),
    goal           TEXT CHECK (char_length(goal) <= 300),
    approach       TEXT CHECK (char_length(approach) <= 500),
    status_text    TEXT CHECK (char_length(status_text) <= 500),
    open_questions TEXT[] NOT NULL DEFAULT '{}' CHECK (bounded_list(open_questions, 5, 300)),
    blockers       TEXT[] NOT NULL DEFAULT '{}' CHECK (bounded_list(blockers, 5, 300)),
    next_actions   TEXT[] NOT NULL DEFAULT '{}' CHECK (bounded_list(next_actions, 5, 300)),
    evidence       UUID[] NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by     TEXT NOT NULL
);
CREATE INDEX task_checkpoint_task ON task_checkpoint (task_id);

-- Record task attempts and outcomes (5.4).
CREATE TABLE attempt (
    attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id    UUID NOT NULL REFERENCES task(task_id),
    attempt    TEXT NOT NULL CHECK (char_length(attempt) <= 300),
    result     TEXT CHECK (char_length(result) <= 500),
    reason     TEXT CHECK (char_length(reason) <= 500),
    next       TEXT CHECK (char_length(next) <= 300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL
);
CREATE INDEX attempt_task ON attempt (task_id);

-- Record task decisions and optional supersession links (5.4).
CREATE TABLE decision (
    decision_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id       UUID NOT NULL REFERENCES task(task_id),
    decision      TEXT NOT NULL CHECK (char_length(decision) <= 300),
    reason        TEXT CHECK (char_length(reason) <= 500),
    supersedes_id UUID REFERENCES decision(decision_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by    TEXT NOT NULL
);
CREATE INDEX decision_task ON decision (task_id);

-- Store artifact references, not artifact bodies (5.5).
CREATE TABLE artifact_reference (
    reference_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID NOT NULL REFERENCES task(task_id),
    kind         TEXT NOT NULL CHECK (kind IN ('git_commit', 'git_branch', 'file', 'document',
                                               'obsidian', 'issue', 'dataset', 'log', 'url',
                                               'other')),
    locator      TEXT NOT NULL CHECK (char_length(locator) <= 500),
    label        TEXT CHECK (char_length(label) <= 300),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX artifact_reference_task ON artifact_reference (task_id);

-- Keep task history tables append-only; task_state holds the current state.
CREATE TRIGGER task_checkpoint_append_only
    BEFORE UPDATE OR DELETE ON task_checkpoint
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER task_checkpoint_no_truncate
    BEFORE TRUNCATE ON task_checkpoint
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER attempt_append_only
    BEFORE UPDATE OR DELETE ON attempt
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER attempt_no_truncate
    BEFORE TRUNCATE ON attempt
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER decision_append_only
    BEFORE UPDATE OR DELETE ON decision
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER decision_no_truncate
    BEFORE TRUNCATE ON decision
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER artifact_reference_append_only
    BEFORE UPDATE OR DELETE ON artifact_reference
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER artifact_reference_no_truncate
    BEFORE TRUNCATE ON artifact_reference
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
