-- A decision may replace several earlier ones at once, so a task holding more decisions in
-- force than its cap can fold them into one (project-state 5.4). The links move out of the
-- decision row because one column holds one id.
CREATE TABLE decision_supersession (
    decision_id   UUID NOT NULL REFERENCES decision(decision_id),
    supersedes_id UUID NOT NULL REFERENCES decision(decision_id),
    PRIMARY KEY (decision_id, supersedes_id),
    CHECK (decision_id <> supersedes_id)
);
CREATE INDEX decision_supersession_target ON decision_supersession (supersedes_id);

INSERT INTO decision_supersession (decision_id, supersedes_id)
SELECT decision_id, supersedes_id FROM decision WHERE supersedes_id IS NOT NULL;

ALTER TABLE decision DROP COLUMN supersedes_id;

CREATE TRIGGER decision_supersession_append_only
    BEFORE UPDATE OR DELETE ON decision_supersession
    FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
CREATE TRIGGER decision_supersession_no_truncate
    BEFORE TRUNCATE ON decision_supersession
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_mutation();
