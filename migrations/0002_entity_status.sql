-- Specification 20.1 and 26 (v0.4).
--
-- An entity gains a status of its own, separate from the status its versions
-- carry. It is what gives an entity created above the similarity threshold a
-- defined state while its review is pending: it exists and can hold versions,
-- but it stays out of the active layer of retrieval.

ALTER TABLE memory_entity
    ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'provisional', 'merged', 'archived')),
    ADD COLUMN merged_into UUID REFERENCES memory_entity(memory_id);

-- A merge target belongs to a merged entity and to no other kind.
ALTER TABLE memory_entity
    ADD CONSTRAINT merged_into_matches_status
        CHECK ((status = 'merged') = (merged_into IS NOT NULL)),
    ADD CONSTRAINT merged_into_is_another_entity
        CHECK (merged_into IS NULL OR merged_into <> memory_id);

-- Only entities in the active status reach layer 1, so that is the lookup the
-- retrieval pipeline makes on every query.
CREATE INDEX idx_entity_retrievable ON memory_entity (scope_id)
    WHERE status = 'active';

-- Specification 26 (v0.4): a disproven version without a reason leaves layer 3
-- with nothing to hand the agent, and fails the traceability criterion in
-- specification 31.
ALTER TABLE memory_version
    ADD CONSTRAINT disproven_needs_reason
        CHECK (status <> 'disproven' OR reason IS NOT NULL);
