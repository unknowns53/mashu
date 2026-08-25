-- Mashu v2 schema (specification docs/mashu-v2.md, section 9).
-- Nine tables. No pgvector: the only matching this layer does is trigram
-- similarity between short pieces of prose, which pg_trgm carries.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE scope (
    scope_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL UNIQUE,
    summary    TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A route to NULL is an answer too: "this directory is deliberately not
-- captured". Only the absence of a row is a gap for a person to fill.
CREATE TABLE route (
    route_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path_prefix TEXT NOT NULL UNIQUE,
    scope_id    UUID REFERENCES scope(scope_id),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per pain (4.1). Append-only: evidence that has been cited must
-- keep existing, so nothing here is ever updated or deleted.
CREATE TABLE ledger (
    ledger_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL CHECK (kind IN ('incident', 'friction', 'explicit')),
    what       TEXT NOT NULL,
    prevention TEXT NOT NULL,
    scope_id   UUID REFERENCES scope(scope_id),
    source     TEXT,
    from_trace UUID,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ledger_prevention_trgm ON ledger USING gin (prevention gin_trgm_ops);

-- Dated observations, not knowledge (4.2). Expired rows are filtered on
-- read rather than deleted; anything cited as evidence has already been
-- frozen into the ledger by then.
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

-- A memory without evidence cannot exist (5): the array is the reason this
-- row is allowed to occupy a seat. NULL scope means the whole ledger; the
-- delivery checks below are the shape of section 6.
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

-- Every content a memory has ever carried, including its first. Append-only.
CREATE TABLE memory_revision (
    revision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id   UUID NOT NULL REFERENCES memory(memory_id),
    content     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Admission candidates (5.1). Nothing an agent writes reaches another
-- session without a human key-press, and this table is where it waits.
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

-- Conditions that stop applying on their own (7). The 14-day ceiling is a
-- constraint rather than advice: a longer claim is an indefinite claim
-- wearing an expiry, and belongs in the evidence pipeline instead.
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

-- Append-only is enforced by the database, not promised by the code.
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
