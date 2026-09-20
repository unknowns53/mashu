ALTER TABLE memory
    ADD COLUMN retirement_kind TEXT,
    ADD COLUMN superseded_by UUID REFERENCES memory(memory_id),
    ADD COLUMN relocated_to_kind TEXT,
    ADD COLUMN relocated_to_id UUID;

UPDATE memory m
SET retirement_kind = 'legacy'
WHERE m.status = 'retired';

UPDATE memory m
SET retirement_kind = 'relocated',
    relocated_to_kind = 'temporary_context',
    relocated_to_id = (
        SELECT (e.detail ->> 'context_id')::uuid
        FROM event_log e
        JOIN temporary_context t ON t.context_id = (e.detail ->> 'context_id')::uuid
        WHERE e.event_type = 'memory_converted_to_temporary'
          AND e.memory_id = m.memory_id
          AND e.detail ? 'context_id'
        ORDER BY e.event_id DESC
        LIMIT 1
    )
WHERE m.status = 'retired'
  AND EXISTS (
      SELECT 1
      FROM event_log e
      JOIN temporary_context t ON t.context_id = (e.detail ->> 'context_id')::uuid
      WHERE e.event_type = 'memory_converted_to_temporary'
        AND e.memory_id = m.memory_id
        AND e.detail ? 'context_id'
  );

ALTER TABLE memory
    ADD CONSTRAINT memory_retirement_kind_check
        CHECK (retirement_kind IS NULL OR retirement_kind IN
               ('invalidated', 'superseded', 'out_of_scope', 'relocated', 'legacy')),
    ADD CONSTRAINT memory_retirement_metadata_check
        CHECK (
            (status = 'active' AND retirement_kind IS NULL
             AND superseded_by IS NULL AND relocated_to_kind IS NULL
             AND relocated_to_id IS NULL)
            OR
            (status = 'retired' AND retirement_kind IS NOT NULL
             AND ((retirement_kind = 'superseded') = (superseded_by IS NOT NULL))
             AND ((retirement_kind = 'relocated') =
                  (relocated_to_kind IS NOT NULL AND relocated_to_id IS NOT NULL))
             AND (relocated_to_kind IS NULL OR relocated_to_kind = 'temporary_context')
             AND (superseded_by IS NULL OR superseded_by <> memory_id))
        );

ALTER TABLE memory
    ADD CONSTRAINT memory_relocation_pair_check
        CHECK ((relocated_to_kind IS NULL) = (relocated_to_id IS NULL));

CREATE UNIQUE INDEX memory_revision_owner_id ON memory_revision (memory_id, revision_id);

ALTER TABLE nomination
    ADD COLUMN approval_source JSONB,
    ADD COLUMN admit_request_id UUID UNIQUE,
    ADD COLUMN admit_request JSONB,
    ADD COLUMN admit_result JSONB,
    ADD COLUMN conflict_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE nomination n
SET conflict_snapshot = COALESCE(
    (
        SELECT jsonb_agg(jsonb_build_object(
            'memory_id', m.memory_id,
            'retirement_kind', m.retirement_kind,
            'retire_reason', m.retire_reason,
            'superseded_by', m.superseded_by,
            'relocated_to_kind', m.relocated_to_kind,
            'relocated_to_id', m.relocated_to_id
        ) ORDER BY m.memory_id)
        FROM memory m
        WHERE m.memory_id = ANY(n.conflicts)
    ),
    '[]'::jsonb
);

CREATE TABLE memory_change (
    change_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_memory_id          UUID NOT NULL REFERENCES memory(memory_id),
    target_revision_id        UUID NOT NULL,
    target_updated_at         TIMESTAMPTZ NOT NULL,
    operation                 TEXT NOT NULL CHECK (operation IN ('retire', 'replace', 'restore')),
    retirement_kind           TEXT CHECK (retirement_kind IN
                                  ('invalidated', 'superseded', 'out_of_scope',
                                   'relocated', 'legacy')),
    retire_reason             TEXT,
    superseded_by             UUID REFERENCES memory(memory_id),
    relocated_to_kind         TEXT,
    relocated_to_id           UUID,
    successor_nomination_id   UUID REFERENCES nomination(nomination_id),
    restore_reason            TEXT,
    evidence                  JSONB NOT NULL DEFAULT '[]'::jsonb
                              CHECK (jsonb_typeof(evidence) = 'array'),
    conflict_ids              UUID[] NOT NULL DEFAULT '{}',
    conflict_snapshot         JSONB NOT NULL DEFAULT '[]'::jsonb
                              CHECK (jsonb_typeof(conflict_snapshot) = 'array'),
    proposed_by                TEXT NOT NULL,
    proposed_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    version                    INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    status                     TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending', 'applied', 'declined', 'withdrawn')),
    decided_by                 TEXT,
    decided_at                 TIMESTAMPTZ,
    decision_reason            TEXT,
    approval_source            JSONB,
    apply_request_id           UUID UNIQUE,
    apply_request              JSONB,
    applied_result             JSONB,
    CHECK ((operation IN ('retire', 'replace')) =
           (retirement_kind IS NOT NULL AND retire_reason IS NOT NULL)),
    CHECK ((operation = 'replace') = (successor_nomination_id IS NOT NULL)),
    CHECK ((operation = 'restore') = (restore_reason IS NOT NULL)),
    CHECK (operation <> 'replace' OR retirement_kind = 'superseded'),
    CHECK (retirement_kind <> 'superseded' OR operation = 'replace'),
    CHECK ((operation = 'replace' AND status = 'applied') =
           (superseded_by IS NOT NULL)),
    CHECK ((retirement_kind = 'relocated') =
           (relocated_to_kind IS NOT NULL AND relocated_to_id IS NOT NULL)),
    CHECK ((relocated_to_kind IS NULL) = (relocated_to_id IS NULL)),
    CHECK (relocated_to_kind IS NULL OR relocated_to_kind = 'temporary_context'),
    CHECK (superseded_by IS NULL OR superseded_by <> target_memory_id),
    FOREIGN KEY (target_memory_id, target_revision_id)
        REFERENCES memory_revision (memory_id, revision_id)
);

CREATE INDEX memory_change_pending ON memory_change (proposed_at, change_id)
    WHERE status = 'pending';

INSERT INTO event_log (event_type, actor, memory_id, detail)
SELECT 'memory_retirement_classified', 'migration', m.memory_id,
       jsonb_build_object('retirement_kind', m.retirement_kind,
                          'retire_reason', m.retire_reason,
                          'relocated_to_kind', m.relocated_to_kind,
                          'relocated_to_id', m.relocated_to_id)
FROM memory m
WHERE m.status = 'retired';
