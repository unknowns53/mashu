-- Where the knowledge behind a pain already was, as the reporter names it (docs/mashu-v2.md
-- 4.1): a task whose state held it, optionally the decision in it, or an instruction file or
-- skill. Knowledge that was there is not missing knowledge, so such a pain is counted here
-- and never becomes a candidate.
-- decision is append-only, so its id cannot dangle and needs no foreign key; ledger.py
-- checks that it exists and belongs to the task.
ALTER TABLE ledger
    ADD COLUMN ignored_task_id UUID REFERENCES task(task_id),
    ADD COLUMN ignored_decision_id UUID,
    ADD COLUMN ignored_source TEXT
        CHECK (ignored_source IS NULL
               OR (ignored_source = btrim(ignored_source)
                   AND char_length(ignored_source) BETWEEN 1 AND 200)),
    ADD CONSTRAINT ledger_ignored_decision_has_task
        CHECK (ignored_decision_id IS NULL OR ignored_task_id IS NOT NULL);

-- A successor body an agent drafts for its own replace proposal (docs/mashu-v2.md 4.1). It is
-- not a user instruction, so it is not recorded as `claimed` / `user_explicit`.
ALTER TABLE ledger DROP CONSTRAINT ledger_kind_check;
ALTER TABLE ledger ADD CONSTRAINT ledger_kind_check
    CHECK (kind IN ('incident', 'friction', 'explicit', 'claimed', 'proposed'));
ALTER TABLE nomination DROP CONSTRAINT nomination_kind_check;
ALTER TABLE nomination ADD CONSTRAINT nomination_kind_check
    CHECK (kind IN ('incident', 'rederivation', 'user_explicit', 'proposed'));
