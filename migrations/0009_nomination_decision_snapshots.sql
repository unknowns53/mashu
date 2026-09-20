ALTER TABLE nomination
    ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0);

CREATE FUNCTION increment_nomination_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.content, NEW.scope_id, NEW.kind, NEW.evidence, NEW.conflicts, NEW.conflict_snapshot)
       IS DISTINCT FROM
       ROW(OLD.content, OLD.scope_id, OLD.kind, OLD.evidence, OLD.conflicts, OLD.conflict_snapshot)
    THEN
        NEW.version := OLD.version + 1;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER nomination_decision_version
    BEFORE UPDATE OF content, scope_id, kind, evidence, conflicts, conflict_snapshot
    ON nomination
    FOR EACH ROW EXECUTE FUNCTION increment_nomination_version();

ALTER TABLE memory_change
    ADD COLUMN successor_snapshot JSONB,
    ADD COLUMN successor_delivery TEXT,
    ADD COLUMN successor_scope_id UUID REFERENCES scope(scope_id),
    ADD COLUMN successor_guard_action TEXT;

UPDATE memory_change change
SET successor_snapshot = jsonb_build_object(
        'version', nomination.version,
        'content', nomination.content,
        'scope_id', nomination.scope_id,
        'kind', nomination.kind,
        'evidence', nomination.evidence,
        'conflicts', nomination.conflicts,
        'conflict_snapshot', nomination.conflict_snapshot
    ),
    successor_delivery = memory.delivery,
    successor_scope_id = memory.scope_id,
    successor_guard_action = memory.guard_action
FROM nomination, memory
WHERE change.operation = 'replace'
  AND nomination.nomination_id = change.successor_nomination_id
  AND memory.memory_id = change.target_memory_id;

ALTER TABLE memory_change
    ADD CONSTRAINT memory_change_successor_snapshot_check
        CHECK ((operation = 'replace') = (successor_snapshot IS NOT NULL)),
    ADD CONSTRAINT memory_change_successor_delivery_check
        CHECK ((operation = 'replace') = (successor_delivery IS NOT NULL)),
    ADD CONSTRAINT memory_change_successor_delivery_value_check
        CHECK (successor_delivery IS NULL OR successor_delivery IN ('always', 'scope', 'guard')),
    ADD CONSTRAINT memory_change_successor_scope_check
        CHECK (successor_delivery <> 'scope' OR successor_scope_id IS NOT NULL),
    ADD CONSTRAINT memory_change_successor_guard_check
        CHECK (successor_delivery IS NULL OR
               ((successor_delivery = 'guard') = (successor_guard_action IS NOT NULL)));

UPDATE nomination n
SET admit_result = to_jsonb(m)
FROM memory m
WHERE n.admit_result IS NOT NULL
  AND n.memory_id = m.memory_id;
