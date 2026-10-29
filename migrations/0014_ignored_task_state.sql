-- The task whose state the reporter says covered the pain and was read but not used, and
-- optionally the decision in it (docs/mashu-v2.md 4.1). Such a pain indicts how task state
-- is handed over, so it is counted here and never becomes a candidate.
-- decision is append-only, so its id cannot dangle and needs no foreign key; ledger.py
-- checks that it exists and belongs to the task.
ALTER TABLE ledger
    ADD COLUMN ignored_task_id UUID REFERENCES task(task_id),
    ADD COLUMN ignored_decision_id UUID,
    ADD CONSTRAINT ledger_ignored_decision_has_task
        CHECK (ignored_decision_id IS NULL OR ignored_task_id IS NOT NULL);
