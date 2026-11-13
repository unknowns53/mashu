-- A task keeps the user's rulings, each with the user's own words, in place of the attempts and
-- decisions agents wrote without review (project-state 5.4). Attempts and decisions stay as a
-- read-only record. Withdrawing a ruling takes the user's words of its own, so one instruction
-- cannot add a ruling and evict an unrelated one.
CREATE TABLE ruling (
    ruling_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id          UUID NOT NULL REFERENCES task(task_id),
    ruling           TEXT NOT NULL CHECK (char_length(ruling) <= 1000 AND btrim(ruling) <> ''),
    user_instruction TEXT NOT NULL
        CHECK (char_length(user_instruction) <= 1000 AND btrim(user_instruction) <> ''),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_by       TEXT NOT NULL
);
CREATE INDEX ruling_task ON ruling (task_id);

CREATE TABLE ruling_withdrawal (
    ruling_id        UUID PRIMARY KEY REFERENCES ruling(ruling_id),
    user_instruction TEXT NOT NULL
        CHECK (char_length(user_instruction) <= 1000 AND btrim(user_instruction) <> ''),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_by       TEXT NOT NULL
);

CREATE TRIGGER ruling_append_only
    BEFORE UPDATE OR DELETE ON ruling
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ruling_no_truncate
    BEFORE TRUNCATE ON ruling
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ruling_withdrawal_append_only
    BEFORE UPDATE OR DELETE ON ruling_withdrawal
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ruling_withdrawal_no_truncate
    BEFORE TRUNCATE ON ruling_withdrawal
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();

-- The rulings' size is the user's to set. The decision share no longer exists.
DELETE FROM setting WHERE name = 'decision_capacity';
ALTER TABLE setting DROP CONSTRAINT setting_name_check;
ALTER TABLE setting DROP CONSTRAINT setting_value_check;
ALTER TABLE setting ADD CONSTRAINT setting_name_check CHECK (name IN (
    'checkpoint_interval_minutes',
    'total_capacity', 'memory_capacity', 'always_capacity', 'topic_capacity',
    'project_capacity', 'card_overflow_capacity', 'task_card_capacity',
    'task_detail_capacity', 'temporary_capacity', 'content_limit',
    'ruling_count', 'ruling_chars', 'ruling_words_chars'
));
ALTER TABLE setting ADD CONSTRAINT setting_value_check CHECK (CASE name
    WHEN 'checkpoint_interval_minutes' THEN value BETWEEN 0 AND 1440
    WHEN 'content_limit' THEN value BETWEEN 20 AND 1000
    WHEN 'ruling_count' THEN value BETWEEN 1 AND 50
    WHEN 'ruling_chars' THEN value BETWEEN 20 AND 1000
    WHEN 'ruling_words_chars' THEN value BETWEEN 20 AND 1000
    ELSE value BETWEEN 50 AND 20000
END);
