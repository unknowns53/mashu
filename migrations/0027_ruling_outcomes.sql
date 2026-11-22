-- A ruling leaves active delivery in three distinct ways (project-state 5.4):
--   ruling_withdrawal   the user dropped it (requires the user's words)
--   ruling_settlement   codified in code, docs, a skill, or memory, or a finished one-off step
--   ruling_supersession replaced by a newer ruling on the same task
-- A ruling can end at most once across all three tables.

CREATE TABLE ruling_settlement (
    ruling_id  UUID PRIMARY KEY REFERENCES ruling(ruling_id),
    settled_in TEXT NOT NULL
        CHECK (char_length(settled_in) <= 500 AND btrim(settled_in) <> ''),
    note       TEXT CHECK (note IS NULL OR (char_length(note) <= 300 AND btrim(note) <> '')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_by TEXT NOT NULL
);

CREATE TABLE ruling_supersession (
    ruling_id     UUID NOT NULL REFERENCES ruling(ruling_id),
    supersedes_id UUID PRIMARY KEY REFERENCES ruling(ruling_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_by    TEXT NOT NULL,
    CHECK (ruling_id <> supersedes_id)
);
CREATE INDEX ruling_supersession_successor ON ruling_supersession (ruling_id);

CREATE TRIGGER ruling_settlement_append_only
    BEFORE UPDATE OR DELETE ON ruling_settlement
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ruling_settlement_no_truncate
    BEFORE TRUNCATE ON ruling_settlement
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();

CREATE TRIGGER ruling_supersession_append_only
    BEFORE UPDATE OR DELETE ON ruling_supersession
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER ruling_supersession_no_truncate
    BEFORE TRUNCATE ON ruling_supersession
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();

CREATE FUNCTION assert_ruling_in_force(target UUID, skip_table TEXT) RETURNS VOID AS $$
BEGIN
    IF skip_table <> 'ruling_withdrawal'
       AND EXISTS (SELECT 1 FROM ruling_withdrawal WHERE ruling_id = target) THEN
        RAISE EXCEPTION 'ruling % is already withdrawn', target;
    END IF;
    IF skip_table <> 'ruling_settlement'
       AND EXISTS (SELECT 1 FROM ruling_settlement WHERE ruling_id = target) THEN
        RAISE EXCEPTION 'ruling % is already settled', target;
    END IF;
    IF skip_table <> 'ruling_supersession'
       AND EXISTS (SELECT 1 FROM ruling_supersession WHERE supersedes_id = target) THEN
        RAISE EXCEPTION 'ruling % is already superseded', target;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION ensure_ruling_ends_once() RETURNS TRIGGER AS $$
BEGIN
    PERFORM assert_ruling_in_force(NEW.ruling_id, TG_TABLE_NAME);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION ensure_ruling_supersession_valid() RETURNS TRIGGER AS $$
DECLARE
    successor_task UUID;
    target_task UUID;
BEGIN
    SELECT task_id INTO successor_task FROM ruling WHERE ruling_id = NEW.ruling_id;
    SELECT task_id INTO target_task FROM ruling WHERE ruling_id = NEW.supersedes_id;
    IF successor_task IS DISTINCT FROM target_task THEN
        RAISE EXCEPTION 'ruling % belongs to another task', NEW.supersedes_id;
    END IF;
    PERFORM assert_ruling_in_force(NEW.supersedes_id, TG_TABLE_NAME);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ruling_withdrawal_ends_once
    BEFORE INSERT ON ruling_withdrawal
    FOR EACH ROW EXECUTE FUNCTION ensure_ruling_ends_once();
CREATE TRIGGER ruling_settlement_ends_once
    BEFORE INSERT ON ruling_settlement
    FOR EACH ROW EXECUTE FUNCTION ensure_ruling_ends_once();
CREATE TRIGGER ruling_supersession_ends_once
    BEFORE INSERT ON ruling_supersession
    FOR EACH ROW EXECUTE FUNCTION ensure_ruling_supersession_valid();
