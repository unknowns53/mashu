-- Project State (specification docs/mashu-v3.md, sections 5, 7, 14).
--
-- Seven tables beside the nine v2 already has, in the same schema. The line
-- between knowledge and work state is held by the module boundary
-- (projects.py / tasks.py), not by a PostgreSQL namespace: with the first
-- nine tables already in public, a split starting here would name a boundary
-- that half the store does not observe.
--
-- What is deliberately absent is a dormancy column. active and dormant are
-- read off the lease at query time (7), so there is no transition to perform,
-- no worker to keep running, and no race between the two states to lose.

CREATE TABLE project (
    project_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    scope_id    UUID REFERENCES scope(scope_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);

-- A semantic unit of work, not one request (5.2). Only two statuses are
-- stored; the third and fourth words a reader uses for a task — active and
-- dormant — are the lease being compared with now().
--
-- outcome is the whole of what 'closed' means here, so the two are the same
-- fact written twice and the checks keep them from disagreeing. close_reason
-- is optional because 'completed' usually explains itself.
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

-- Whether a list of short lines stays inside its two ceilings, as one
-- expression a CHECK can hold: a constraint may not carry a subquery of its
-- own, and a per-element limit needs one.
CREATE FUNCTION bounded_list(items TEXT[], max_items INT, max_chars INT)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(cardinality(items), 0) <= max_items
       AND coalesce((SELECT max(char_length(item)) FROM unnest(items) AS item), 0) <= max_chars
$$;

-- The "now" of one task and nothing else (5.3). One row per task, replaced in
-- place: a state that kept its own history would be a work diary, and the
-- reader's budget is what a work diary spends first.
--
-- The character limits are the store enforcing what an instruction cannot.
-- "Write concisely" does not control volume; this does. The service layer
-- refuses first so the caller gets a sentence naming the field, and these are
-- the wall standing behind that.
--
-- updated_at is read back as the token a concurrent replacement is checked
-- against (5.3), so it is clock_timestamp() rather than now(): now() is one
-- value for a whole transaction, and a token that does not change when the
-- row does is not a token.
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

-- A break in the work: the replacement of the current state and the freezing
-- of what it said, in one act (5.6). The frozen columns duplicate task_state
-- on purpose — the point of the row is that it does not change when the
-- state does. evidence names artifact_reference rows, as an untyped array for
-- the same reason memory.evidence is one.
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

-- What was tried and what came of it (5.4). The limits are the prohibition on
-- copying logs and diffs into the store, in the only form that holds.
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

-- Why something was done that way, kept because re-deriving the reason is
-- what costs (5.4). A decision is revised by a new row pointing back at the
-- old one, never by editing it: the superseded reason is the half of the pair
-- a later reader needs to understand the change.
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

-- Where the real thing lives (5.5). A locator and a label; never the body.
-- Mashu does not absorb what git, Obsidian and the filesystem already hold,
-- and a store that copied them would hold the stale copy of each.
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

-- The history is append-only for the reason the ledger is: the current state
-- is replaced constantly and keeps nothing, so these four rows are the only
-- account of how the work got where it is. A record that can be rewritten
-- afterwards is a narrative. refuse_mutation() comes from 0001.
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
