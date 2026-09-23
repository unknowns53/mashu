-- Topics: rules read when one kind of work begins (docs/mashu-v2.md 6).
-- NULL scope lists the topic in every session.
CREATE TABLE topic (
    topic_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE
                CHECK (name = btrim(name) AND char_length(name) BETWEEN 1 AND 40),
    scope_id    UUID REFERENCES scope(scope_id),
    trigger     TEXT NOT NULL
                CHECK (trigger = btrim(trigger) AND char_length(trigger) BETWEEN 1 AND 160),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);

ALTER TABLE memory ADD COLUMN topic_id UUID REFERENCES topic(topic_id);

ALTER TABLE memory
    DROP CONSTRAINT memory_delivery_check,
    ADD CONSTRAINT memory_delivery_check
        CHECK (delivery IN ('always', 'scope', 'guard', 'topic')),
    ADD CONSTRAINT memory_topic_check
        CHECK ((delivery = 'topic') = (topic_id IS NOT NULL));

CREATE INDEX memory_active_topic ON memory (topic_id) WHERE status = 'active';

-- A proposal names its topic; a trigger means applying it creates that topic.
ALTER TABLE memory_change
    ADD COLUMN successor_topic_id UUID REFERENCES topic(topic_id),
    ADD COLUMN successor_topic_name TEXT,
    ADD COLUMN successor_topic_trigger TEXT;

ALTER TABLE memory_change
    DROP CONSTRAINT memory_change_operation_check,
    ADD CONSTRAINT memory_change_operation_check
        CHECK (operation IN ('retire', 'replace', 'restore', 'redeliver')),
    DROP CONSTRAINT memory_change_successor_delivery_check,
    ADD CONSTRAINT memory_change_successor_delivery_check
        CHECK ((operation IN ('replace', 'redeliver')) = (successor_delivery IS NOT NULL)),
    DROP CONSTRAINT memory_change_successor_delivery_value_check,
    ADD CONSTRAINT memory_change_successor_delivery_value_check
        CHECK (successor_delivery IS NULL
               OR successor_delivery IN ('always', 'scope', 'guard', 'topic')),
    ADD CONSTRAINT memory_change_successor_topic_check
        CHECK ((successor_delivery IS NOT DISTINCT FROM 'topic') =
               (successor_topic_name IS NOT NULL)),
    ADD CONSTRAINT memory_change_successor_topic_source_check
        CHECK (successor_topic_name IS NULL
               OR ((successor_topic_id IS NULL) <> (successor_topic_trigger IS NULL)));
