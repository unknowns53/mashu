-- A scope Memory can be delivered to several scopes (memory 5). memory.scope_id stays its home
-- scope, which candidates, proposals, and the ledger already carry; memory_scope lists the
-- other scopes it also reaches. 0020's scope inclusion is removed.
DROP TRIGGER scope_include_one_level ON scope_include;
DROP TABLE scope_include;
DROP FUNCTION refuse_nested_include();

CREATE TABLE memory_scope (
    memory_id  UUID NOT NULL REFERENCES memory(memory_id),
    scope_id   UUID NOT NULL REFERENCES scope(scope_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, scope_id)
);
CREATE INDEX memory_scope_scope ON memory_scope (scope_id);

-- Only a scope Memory has other scopes, and never its home scope a second time.
CREATE FUNCTION check_memory_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner memory%ROWTYPE;
BEGIN
    SELECT * INTO owner FROM memory WHERE memory_id = NEW.memory_id;
    IF owner.delivery <> 'scope' THEN
        RAISE EXCEPTION 'memory % is delivered %, so it has no other scopes', NEW.memory_id, owner.delivery;
    END IF;
    IF owner.scope_id = NEW.scope_id THEN
        RAISE EXCEPTION 'scope % is already the home scope of memory %', NEW.scope_id, NEW.memory_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memory_scope_checked
    BEFORE INSERT OR UPDATE ON memory_scope
    FOR EACH ROW EXECUTE FUNCTION check_memory_scope();

-- Moving a Memory to another delivery or another home scope is a move, so the scopes it also
-- reached go with the old place. A writer that wants them kept writes them again after the move.
CREATE FUNCTION clear_moved_memory_scopes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.delivery <> 'scope' OR NEW.scope_id IS DISTINCT FROM OLD.scope_id THEN
        DELETE FROM memory_scope WHERE memory_id = NEW.memory_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memory_scope_cleared
    AFTER UPDATE OF delivery, scope_id ON memory
    FOR EACH ROW EXECUTE FUNCTION clear_moved_memory_scopes();

-- A scope that past records name cannot be deleted, since ledger rows are append-only and keep
-- their scope_id. Removing such a scope archives it: it leaves listings, cannot be routed or
-- given new knowledge, and keeps its name.
ALTER TABLE scope ADD COLUMN archived_at TIMESTAMPTZ;
