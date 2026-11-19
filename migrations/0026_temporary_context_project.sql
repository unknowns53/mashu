-- A temporary context can be delivered to several projects, as a project Memory can (memory 6).
-- temporary_context.project_id stays its home project (or NULL for every session), and
-- temporary_context_project lists the other projects it also reaches.
CREATE TABLE temporary_context_project (
    context_id UUID NOT NULL REFERENCES temporary_context(context_id),
    project_id UUID NOT NULL REFERENCES project(project_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (context_id, project_id)
);
CREATE INDEX temporary_context_project_project ON temporary_context_project (project_id);

-- Only a project-scoped temporary context has other projects, and never its home project twice.
CREATE FUNCTION check_temporary_context_project() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner temporary_context%ROWTYPE;
BEGIN
    SELECT * INTO owner FROM temporary_context WHERE context_id = NEW.context_id FOR SHARE;
    IF owner.project_id IS NULL THEN
        RAISE EXCEPTION 'temporary context % reaches every session, so it has no other projects',
            NEW.context_id;
    END IF;
    IF owner.project_id = NEW.project_id THEN
        RAISE EXCEPTION 'project % is already the home project of temporary context %',
            NEW.project_id, NEW.context_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER temporary_context_project_checked
    BEFORE INSERT OR UPDATE ON temporary_context_project
    FOR EACH ROW EXECUTE FUNCTION check_temporary_context_project();

-- Moving a temporary context to every session or another home project clears its extra projects.
-- A writer that wants them kept writes them again after the update.
CREATE FUNCTION clear_moved_temporary_context_projects() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        DELETE FROM temporary_context_project WHERE context_id = NEW.context_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER temporary_context_project_cleared
    AFTER UPDATE OF project_id ON temporary_context
    FOR EACH ROW EXECUTE FUNCTION clear_moved_temporary_context_projects();

-- Replace the creation-relative 14-day CHECK with a write-time trigger so an expiry can be
-- shortened or extended while still enforcing the 14-day window from the time it is set.
DO $$
DECLARE
    constraint_name TEXT;
BEGIN
    FOR constraint_name IN
        SELECT c.conname
        FROM pg_constraint c
        WHERE c.conrelid = 'temporary_context'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) LIKE '%expires_at%'
          AND pg_get_constraintdef(c.oid) LIKE '%created_at%'
    LOOP
        EXECUTE format('ALTER TABLE temporary_context DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END;
$$;

CREATE FUNCTION check_temporary_context_expiry() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.expires_at > now() + INTERVAL '14 days' THEN
        RAISE EXCEPTION 'temporary context expiry cannot be more than 14 days from now';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER temporary_context_expiry_checked
    BEFORE INSERT OR UPDATE OF expires_at ON temporary_context
    FOR EACH ROW EXECUTE FUNCTION check_temporary_context_expiry();
