-- Guard rules become a topic linked to the action they stood before (docs/mashu-v2.md 6.2).
-- NULL action links the topic to no tool call.
ALTER TABLE topic
    ADD COLUMN action TEXT
        CHECK (action IS NULL OR action ~ '^[A-Za-z0-9_.-]{1,40}$');

CREATE UNIQUE INDEX topic_open_action ON topic (action)
    WHERE archived_at IS NULL AND action IS NOT NULL;

DO $$
DECLARE
    guarded RECORD;
    opened  UUID;
BEGIN
    IF EXISTS (
        SELECT 1 FROM memory_change
        WHERE status = 'pending' AND successor_delivery = 'guard'
    ) THEN
        RAISE EXCEPTION 'pending memory changes still propose delivery guard; withdraw them '
            'with mashu review --changes, then run the migration again';
    END IF;

    FOR guarded IN
        SELECT guard_action AS action,
               count(DISTINCT coalesce(scope_id::text, '')) AS scopes,
               (array_agg(scope_id))[1] AS scope_id
        FROM memory
        WHERE delivery = 'guard'
        GROUP BY guard_action
        ORDER BY guard_action
    LOOP
        IF guarded.action !~ '^[A-Za-z0-9_.-]{1,40}$' THEN
            RAISE EXCEPTION 'guard action % cannot name a topic (letters, digits, _ . - and at '
                'most 40 characters); move its memories to another delivery first',
                quote_literal(guarded.action);
        END IF;
        IF guarded.scopes > 1 THEN
            RAISE EXCEPTION 'guard memories for action % sit in more than one scope, and one '
                'topic has one scope; give them the same scope first', quote_literal(guarded.action);
        END IF;
        IF EXISTS (SELECT 1 FROM topic WHERE name = guarded.action) THEN
            RAISE EXCEPTION 'a topic named % already exists, so the guard memories for that '
                'action have nowhere to go; rename that topic first', quote_literal(guarded.action);
        END IF;

        INSERT INTO topic (name, scope_id, trigger, action, created_by)
        VALUES (guarded.action, guarded.scope_id,
                'before the ' || guarded.action || ' action', guarded.action, 'migration')
        RETURNING topic_id INTO opened;

        -- updated_at stays, so proposals read against these rules remain applicable; each
        -- rule still reaches the same action.
        UPDATE memory
        SET delivery = 'topic', topic_id = opened, guard_action = NULL
        WHERE delivery = 'guard' AND guard_action = guarded.action;

        INSERT INTO event_log (event_type, actor, detail)
        VALUES ('topic_created', 'migration',
                jsonb_build_object('topic_id', opened,
                                   'name', guarded.action,
                                   'trigger', 'before the ' || guarded.action || ' action',
                                   'scope_id', guarded.scope_id,
                                   'action', guarded.action));
    END LOOP;
END $$;

-- Dropping the column also drops the unnamed CHECK that paired it with delivery 'guard'.
ALTER TABLE memory
    DROP CONSTRAINT memory_delivery_check,
    ADD CONSTRAINT memory_delivery_check
        CHECK (delivery IN ('always', 'scope', 'topic')),
    DROP COLUMN guard_action;
