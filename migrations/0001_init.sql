-- Mashu v2 schema (specification docs/mashu-v2.md, section 9).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE scope (
    scope_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL UNIQUE,
    summary    TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NULL scope marks a directory as intentionally uncaptured.
CREATE TABLE route (
    route_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path_prefix TEXT NOT NULL UNIQUE,
    scope_id    UUID REFERENCES scope(scope_id),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reported pains; append-only (4.1).
CREATE TABLE ledger (
    ledger_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL CHECK (kind IN ('incident', 'friction', 'explicit', 'claimed')),
    what       TEXT NOT NULL,
    prevention TEXT NOT NULL,
    scope_id   UUID REFERENCES scope(scope_id),
    source     TEXT,
    from_trace UUID,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ledger_prevention_trgm ON ledger USING gin (prevention gin_trgm_ops);

-- Dated, expiring observations (4.2).
CREATE TABLE trace (
    trace_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content    TEXT NOT NULL,
    scope_id   UUID REFERENCES scope(scope_id),
    source     TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX trace_content_trgm ON trace USING gin (content gin_trgm_ops);

-- Every memory must cite evidence (5); NULL scope means global delivery.
CREATE TABLE memory (
    memory_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content       TEXT NOT NULL,
    scope_id      UUID REFERENCES scope(scope_id),
    delivery      TEXT NOT NULL CHECK (delivery IN ('always', 'scope', 'guard')),
    guard_action  TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    evidence      UUID[] NOT NULL CHECK (cardinality(evidence) >= 1),
    retire_reason TEXT,
    retired_at    TIMESTAMPTZ,
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (delivery <> 'scope' OR scope_id IS NOT NULL),
    CHECK ((delivery = 'guard') = (guard_action IS NOT NULL)),
    CHECK ((status = 'retired') = (retire_reason IS NOT NULL))
);
CREATE INDEX memory_content_trgm ON memory USING gin (content gin_trgm_ops);

-- Immutable content revisions for each memory.
CREATE TABLE memory_revision (
    revision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id   UUID NOT NULL REFERENCES memory(memory_id),
    content     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Memory candidates awaiting human review (5.1).
CREATE TABLE nomination (
    nomination_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content         TEXT NOT NULL,
    scope_id        UUID REFERENCES scope(scope_id),
    kind            TEXT NOT NULL CHECK (kind IN ('incident', 'rederivation', 'user_explicit')),
    evidence        UUID[] NOT NULL CHECK (cardinality(evidence) >= 1),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'admitted', 'declined')),
    memory_id       UUID REFERENCES memory(memory_id),
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    decision_reason TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'declined' OR decision_reason IS NOT NULL),
    CHECK (status <> 'admitted' OR memory_id IS NOT NULL)
);
CREATE INDEX nomination_content_trgm ON nomination USING gin (content gin_trgm_ops);

-- Expiring temporary conditions (7).
CREATE TABLE temporary_context (
    context_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content    TEXT NOT NULL,
    scope_id   UUID REFERENCES scope(scope_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at <= created_at + INTERVAL '14 days')
);

CREATE TABLE event_log (
    event_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type    TEXT NOT NULL,
    actor         TEXT NOT NULL,
    memory_id     UUID,
    ledger_id     UUID,
    nomination_id UUID,
    trace_id      UUID,
    detail        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Validate evidence references on write.
CREATE FUNCTION validate_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    absent BIGINT;
BEGIN
    IF array_position(NEW.evidence, NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'evidence contains a NULL element';
    END IF;
    SELECT count(*) INTO absent
    FROM unnest(NEW.evidence) AS cited(ledger_id)
    WHERE NOT EXISTS (
        SELECT 1 FROM ledger l WHERE l.ledger_id = cited.ledger_id
    );
    IF absent > 0 THEN
        RAISE EXCEPTION 'evidence names % ledger row(s) that do not exist', absent;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER nomination_evidence_exists
    BEFORE INSERT OR UPDATE ON nomination
    FOR EACH ROW EXECUTE FUNCTION validate_evidence();
CREATE TRIGGER memory_evidence_exists
    BEFORE INSERT OR UPDATE ON memory
    FOR EACH ROW EXECUTE FUNCTION validate_evidence();

-- Enforce append-only tables in the database.
CREATE FUNCTION refuse_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$;

CREATE TRIGGER event_log_append_only
    BEFORE UPDATE OR DELETE ON event_log
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER event_log_no_truncate
    BEFORE TRUNCATE ON event_log
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ledger_append_only
    BEFORE UPDATE OR DELETE ON ledger
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ledger_no_truncate
    BEFORE TRUNCATE ON ledger
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER memory_revision_append_only
    BEFORE UPDATE OR DELETE ON memory_revision
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER memory_revision_no_truncate
    BEFORE TRUNCATE ON memory_revision
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
