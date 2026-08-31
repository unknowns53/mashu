-- A prevention is not always a rule.
--
-- v2 had one shape for everything that would have stopped a pain: a sentence
-- somebody could be told. That held while there was nowhere else to put an
-- answer. v3 gave the work its own place, and with it a second shape — the
-- fix that is made once, in the code, and is then finished. Recording that as
-- a rule candidate puts a task on the review desk, where the only decisions
-- available are admit, reject and defer, and none of them is 'do it'.
--
-- The ledger row still records what forgetting cost, because that is what the
-- ledger is for and it does not depend on which shape the answer took.
ALTER TABLE ledger
    ADD COLUMN prevention_kind TEXT NOT NULL DEFAULT 'rule'
        CHECK (prevention_kind IN ('rule', 'work'));

-- Where the work was filed, when it was filed anywhere. NULL against a 'work'
-- row is the case worth being able to find: a fix that was named at the
-- moment it was obvious and then landed on no task. Left as a plain reference
-- with no cascade — a task closing does not unfile the work, and the row
-- keeps pointing at where the answer went.
ALTER TABLE ledger
    ADD COLUMN filed_task UUID REFERENCES task(task_id);

CREATE INDEX ledger_unfiled_work ON ledger (created_at DESC)
    WHERE prevention_kind = 'work' AND filed_task IS NULL;
