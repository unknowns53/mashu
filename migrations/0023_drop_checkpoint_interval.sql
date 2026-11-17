-- A session's later writes to a task are no longer answered with a warning, so the interval the
-- user set for it has no reader.
DELETE FROM setting WHERE name = 'checkpoint_interval_minutes';
ALTER TABLE setting DROP CONSTRAINT setting_name_check;
ALTER TABLE setting DROP CONSTRAINT setting_value_check;
ALTER TABLE setting ADD CONSTRAINT setting_name_check CHECK (name IN (
    'total_capacity', 'memory_capacity', 'always_capacity', 'topic_capacity',
    'project_capacity', 'card_overflow_capacity', 'task_card_capacity',
    'task_detail_capacity', 'temporary_capacity', 'content_limit',
    'ruling_count', 'ruling_chars', 'ruling_words_chars'
));
ALTER TABLE setting ADD CONSTRAINT setting_value_check CHECK (CASE name
    WHEN 'content_limit' THEN value BETWEEN 20 AND 1000
    WHEN 'ruling_count' THEN value BETWEEN 1 AND 50
    WHEN 'ruling_chars' THEN value BETWEEN 20 AND 1000
    WHEN 'ruling_words_chars' THEN value BETWEEN 20 AND 1000
    ELSE value BETWEEN 50 AND 20000
END);
