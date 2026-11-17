-- One repository's sessions, their rules, and their tasks are one concept, named project in the
-- tables, the delivery value, and the stored requests and results the code compares against.
-- event_log is append-only history and keeps the names its rows were written with.

-- These triggers name scope columns and the value 'scope' in their bodies. Left in place, the
-- delivery rewrite below would clear every Memory's other scopes and bump candidate versions.
DROP TRIGGER memory_scope_checked ON memory_scope;
DROP TRIGGER memory_scope_cleared ON memory;
DROP TRIGGER nomination_decision_version ON nomination;
DROP FUNCTION check_memory_scope();
DROP FUNCTION clear_moved_memory_scopes();
DROP FUNCTION increment_nomination_version();

-- memory_check is the unnamed CHECK of 0001 pairing delivery 'scope' with a scope_id.
ALTER TABLE memory
    DROP CONSTRAINT memory_check,
    DROP CONSTRAINT memory_delivery_check;
ALTER TABLE memory_change
    DROP CONSTRAINT memory_change_successor_scope_check,
    DROP CONSTRAINT memory_change_successor_delivery_value_check;

UPDATE memory SET delivery = 'project' WHERE delivery = 'scope';
UPDATE memory_change SET successor_delivery = 'project' WHERE successor_delivery = 'scope';

ALTER TABLE scope RENAME TO project;
ALTER TABLE project RENAME COLUMN scope_id TO project_id;
ALTER TABLE memory_scope RENAME TO memory_project;
ALTER TABLE memory_project RENAME COLUMN scope_id TO project_id;
ALTER TABLE route RENAME COLUMN scope_id TO project_id;
ALTER TABLE memory RENAME COLUMN scope_id TO project_id;
ALTER TABLE nomination RENAME COLUMN scope_id TO project_id;
ALTER TABLE temporary_context RENAME COLUMN scope_id TO project_id;
ALTER TABLE topic RENAME COLUMN scope_id TO project_id;
ALTER TABLE ledger RENAME COLUMN scope_id TO project_id;
ALTER TABLE trace RENAME COLUMN scope_id TO project_id;
ALTER TABLE memory_change RENAME COLUMN successor_scope_id TO successor_project_id;

ALTER TABLE memory
    ADD CONSTRAINT memory_delivery_check CHECK (delivery IN ('always', 'project', 'topic')),
    ADD CONSTRAINT memory_project_check CHECK (delivery <> 'project' OR project_id IS NOT NULL);
ALTER TABLE memory_change
    ADD CONSTRAINT memory_change_successor_delivery_value_check
        CHECK (successor_delivery IS NULL
               OR successor_delivery IN ('always', 'project', 'guard', 'topic')),
    ADD CONSTRAINT memory_change_successor_project_check
        CHECK (successor_delivery <> 'project' OR successor_project_id IS NOT NULL);

-- Stored requests and results are compared with new ones on a retry, so their keys follow the
-- columns: every key containing scope takes project in its place, and a delivery or
-- successor_delivery of 'scope' becomes 'project'. Other values are left as written.
CREATE FUNCTION pg_temp.project_keys(doc JSONB) RETURNS JSONB LANGUAGE plpgsql AS $$
BEGIN
    IF jsonb_typeof(doc) = 'object' THEN
        RETURN coalesce(
            (SELECT jsonb_object_agg(
                        replace(key, 'scope', 'project'),
                        CASE WHEN key IN ('delivery', 'successor_delivery') AND value = '"scope"'
                             THEN '"project"'::jsonb
                             ELSE pg_temp.project_keys(value) END)
             FROM jsonb_each(doc)),
            '{}'::jsonb);
    END IF;
    IF jsonb_typeof(doc) = 'array' THEN
        RETURN coalesce(
            (SELECT jsonb_agg(pg_temp.project_keys(value) ORDER BY ordinality)
             FROM jsonb_array_elements(doc) WITH ORDINALITY),
            '[]'::jsonb);
    END IF;
    RETURN doc;
END;
$$;

UPDATE memory_change
SET successor_snapshot = pg_temp.project_keys(successor_snapshot),
    conflict_snapshot = pg_temp.project_keys(conflict_snapshot),
    approval_source = pg_temp.project_keys(approval_source),
    apply_request = pg_temp.project_keys(apply_request),
    applied_result = pg_temp.project_keys(applied_result),
    evidence = pg_temp.project_keys(evidence)
WHERE concat(successor_snapshot, conflict_snapshot, approval_source, apply_request,
             applied_result, evidence) LIKE '%scope%';

UPDATE nomination
SET admit_request = pg_temp.project_keys(admit_request),
    admit_result = pg_temp.project_keys(admit_result),
    conflict_snapshot = pg_temp.project_keys(conflict_snapshot),
    approval_source = pg_temp.project_keys(approval_source)
WHERE concat(admit_request, admit_result, conflict_snapshot, approval_source) LIKE '%scope%';

DROP FUNCTION pg_temp.project_keys(JSONB);

-- Only a project Memory has other projects, and never its home project a second time. The
-- owner row is read under a share lock so a concurrent move of the Memory out of project
-- delivery waits for this row, or this insert waits for the move and sees its result.
CREATE FUNCTION check_memory_project() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner memory%ROWTYPE;
BEGIN
    SELECT * INTO owner FROM memory WHERE memory_id = NEW.memory_id FOR SHARE;
    IF owner.delivery <> 'project' THEN
        RAISE EXCEPTION 'memory % is delivered %, so it has no other projects', NEW.memory_id, owner.delivery;
    END IF;
    IF owner.project_id = NEW.project_id THEN
        RAISE EXCEPTION 'project % is already the home project of memory %', NEW.project_id, NEW.memory_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memory_project_checked
    BEFORE INSERT OR UPDATE ON memory_project
    FOR EACH ROW EXECUTE FUNCTION check_memory_project();

-- Moving a Memory to another delivery or another home project is a move, so the projects it
-- also reached go with the old place. A writer that wants them kept writes them again after it.
CREATE FUNCTION clear_moved_memory_projects() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.delivery <> 'project' OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        DELETE FROM memory_project WHERE memory_id = NEW.memory_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER memory_project_cleared
    AFTER UPDATE OF delivery, project_id ON memory
    FOR EACH ROW EXECUTE FUNCTION clear_moved_memory_projects();

CREATE FUNCTION increment_nomination_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.content, NEW.project_id, NEW.kind, NEW.evidence, NEW.conflicts, NEW.conflict_snapshot)
       IS DISTINCT FROM
       ROW(OLD.content, OLD.project_id, OLD.kind, OLD.evidence, OLD.conflicts, OLD.conflict_snapshot)
    THEN
        NEW.version := OLD.version + 1;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER nomination_decision_version
    BEFORE UPDATE OF content, project_id, kind, evidence, conflicts, conflict_snapshot
    ON nomination
    FOR EACH ROW EXECUTE FUNCTION increment_nomination_version();
