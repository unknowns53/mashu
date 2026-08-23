-- Mashu initial schema. Follows docs/mashu-mvp-v0.3.md section 26.
--
-- Embedding dimension: 1024, matching intfloat/multilingual-e5-large.
-- Changing the model means changing this dimension, which requires a new
-- migration that rebuilds the embedding columns.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- scope: a ledger, not free text, so that spelling drift cannot split a scope.
-- Only the user creates scopes; agents pick from what exists (section 7).
-- ---------------------------------------------------------------------------
CREATE TABLE scope (
    scope_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- memory_entity: the conceptual unit. Holds pointers, never content.
-- "Active" is defined solely as the version active_version points at
-- (section 10); there is no active status.
-- ---------------------------------------------------------------------------
CREATE TABLE memory_entity (
    memory_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id        UUID NOT NULL REFERENCES scope(scope_id),
    type            TEXT NOT NULL
                    CHECK (type IN ('observation', 'fact', 'interpretation',
                                    'hypothesis', 'decision', 'task',
                                    'preference', 'state')),
    title           TEXT NOT NULL,
    active_version  UUID,   -- foreign key added below: the reference is mutual
    latest_version  UUID,
    title_embedding vector(1024),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- memory_version: immutable history. A change is a new row, never an edit.
-- Provenance is folded in as columns; the relation is one to one (section 9).
-- ---------------------------------------------------------------------------
CREATE TABLE memory_version (
    version_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id         UUID NOT NULL REFERENCES memory_entity(memory_id),
    content           TEXT NOT NULL,
    status            TEXT NOT NULL
                      CHECK (status IN ('candidate', 'superseded',
                                        'disproven', 'dormant', 'completed')),
    supersedes        UUID REFERENCES memory_version(version_id),
    reason            TEXT,
    source_type       TEXT NOT NULL
                      CHECK (source_type IN ('user', 'agent', 'tool',
                                             'file', 'web')),
    source_reference  TEXT,
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_embedding vector(1024),

    -- Target for the composite foreign keys on memory_entity below.
    UNIQUE (version_id, memory_id)
);

-- The entity pointers must land on a version of that same entity. A plain
-- foreign key on version_id alone would allow an entity to point at another
-- entity's version, which section 10 rules out but the column types do not.
ALTER TABLE memory_entity
    ADD CONSTRAINT fk_active_version
        FOREIGN KEY (active_version, memory_id)
        REFERENCES memory_version (version_id, memory_id),
    ADD CONSTRAINT fk_latest_version
        FOREIGN KEY (latest_version, memory_id)
        REFERENCES memory_version (version_id, memory_id);

CREATE INDEX idx_entity_scope ON memory_entity (scope_id);
CREATE INDEX idx_version_memory ON memory_version (memory_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- memory_evidence: reference edges only, not a dependency graph (section 19).
-- The reverse index answers "what rests on this memory" in one lookup.
-- ---------------------------------------------------------------------------
CREATE TABLE memory_evidence (
    from_version UUID NOT NULL REFERENCES memory_version(version_id),
    to_memory    UUID NOT NULL REFERENCES memory_entity(memory_id),
    PRIMARY KEY (from_version, to_memory)
);
CREATE INDEX idx_evidence_reverse ON memory_evidence (to_memory);

-- ---------------------------------------------------------------------------
-- proposal: every change enters through here (section 15). based_on_version
-- carries the optimistic lock checked at commit time (section 24).
-- ---------------------------------------------------------------------------
CREATE TABLE proposal (
    proposal_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor            TEXT NOT NULL,
    operation        TEXT NOT NULL
                     CHECK (operation IN ('create', 'update_version',
                                          'change_status', 'restore', 'merge')),
    target_memory    UUID REFERENCES memory_entity(memory_id),
    based_on_version UUID REFERENCES memory_version(version_id),
    payload          JSONB NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'rejected',
                                       'auto_committed')),
    reviewer         TEXT,
    decided_at       TIMESTAMPTZ,
    decision_reason  TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A decided proposal records who decided and why; a pending one does not.
    -- Rejection without a reason would break the traceability success criterion
    -- in section 31.
    CONSTRAINT decision_is_complete CHECK (
        (status = 'pending'  AND reviewer IS NULL AND decided_at IS NULL)
        OR (status = 'rejected' AND decided_at IS NOT NULL
                                AND decision_reason IS NOT NULL)
        OR (status IN ('approved', 'auto_committed') AND decided_at IS NOT NULL)
    )
);

CREATE INDEX idx_proposal_pending ON proposal (created_at)
    WHERE status = 'pending';
CREATE INDEX idx_proposal_target ON proposal (target_memory);

-- ---------------------------------------------------------------------------
-- event_log: append only (section 26). Enforced by trigger rather than by
-- GRANT, because the local single-role setup connects as the table owner and
-- privileges would not bind.
-- ---------------------------------------------------------------------------
CREATE TABLE event_log (
    event_id    BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    proposal_id UUID,
    memory_id   UUID,
    version_id  UUID,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_event_memory ON event_log (memory_id, created_at DESC);
CREATE INDEX idx_event_created ON event_log (created_at DESC);

CREATE FUNCTION event_log_is_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'event_log is append only: % is not permitted', TG_OP;
END;
$$;

CREATE TRIGGER trg_event_log_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON event_log
    FOR EACH STATEMENT EXECUTE FUNCTION event_log_is_append_only();

-- ---------------------------------------------------------------------------
-- conflict: recorded by hand in the MVP; no detection, no auto resolution
-- (section 23).
-- ---------------------------------------------------------------------------
CREATE TABLE conflict (
    conflict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_a    UUID NOT NULL REFERENCES memory_entity(memory_id),
    memory_b    UUID NOT NULL REFERENCES memory_entity(memory_id),
    type        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'resolved', 'dismissed')),
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT conflict_is_between_two CHECK (memory_a <> memory_b)
);

-- ---------------------------------------------------------------------------
-- agent_session: scratch lives here and is not long term memory (section 25).
-- ---------------------------------------------------------------------------
CREATE TABLE agent_session (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent      TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,
    scratch    JSONB
);
