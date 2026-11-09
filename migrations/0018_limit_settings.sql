-- Delivery limits move to the Settings screen for the same reason the checkpoint interval did:
-- each agent session's MCP server keeps the environment it started with. A missing row means
-- the default. The ranges mirror settings.LIMITS.
ALTER TABLE setting DROP CONSTRAINT setting_name_check;
ALTER TABLE setting DROP CONSTRAINT setting_value_check;

ALTER TABLE setting ADD CONSTRAINT setting_name_check CHECK (name IN (
    'checkpoint_interval_minutes',
    'total_capacity', 'memory_capacity', 'always_capacity', 'topic_capacity',
    'project_capacity', 'card_overflow_capacity', 'task_card_capacity',
    'task_detail_capacity', 'decision_capacity', 'temporary_capacity', 'content_limit'
));
ALTER TABLE setting ADD CONSTRAINT setting_value_check CHECK (CASE name
    WHEN 'checkpoint_interval_minutes' THEN value BETWEEN 0 AND 1440
    WHEN 'content_limit' THEN value BETWEEN 20 AND 1000
    ELSE value BETWEEN 50 AND 20000
END);
