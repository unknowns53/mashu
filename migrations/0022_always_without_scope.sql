-- An always Memory reaches every session, so it has no home scope (memory 4). A scope_id left on
-- one held that scope against removal although nothing was delivered there.
UPDATE memory SET scope_id = NULL WHERE delivery = 'always' AND scope_id IS NOT NULL;
ALTER TABLE memory ADD CHECK (delivery <> 'always' OR scope_id IS NULL);

-- The owner row is read under a share lock so a concurrent move of the Memory out of scope
-- delivery waits for this row, or this insert waits for the move and sees its result.
CREATE OR REPLACE FUNCTION check_memory_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner memory%ROWTYPE;
BEGIN
    SELECT * INTO owner FROM memory WHERE memory_id = NEW.memory_id FOR SHARE;
    IF owner.delivery <> 'scope' THEN
        RAISE EXCEPTION 'memory % is delivered %, so it has no other scopes', NEW.memory_id, owner.delivery;
    END IF;
    IF owner.scope_id = NEW.scope_id THEN
        RAISE EXCEPTION 'scope % is already the home scope of memory %', NEW.scope_id, NEW.memory_id;
    END IF;
    RETURN NEW;
END;
$$;
