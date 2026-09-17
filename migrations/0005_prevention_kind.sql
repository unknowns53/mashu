-- Distinguish reusable rules from one-time work fixes.
ALTER TABLE ledger
    ADD COLUMN prevention_kind TEXT NOT NULL DEFAULT 'rule'
        CHECK (prevention_kind IN ('rule', 'work'));

-- Optional task reference for work fixes.
ALTER TABLE ledger
    ADD COLUMN filed_task UUID REFERENCES task(task_id);

CREATE INDEX ledger_unfiled_work ON ledger (created_at DESC)
    WHERE prevention_kind = 'work' AND filed_task IS NULL;
