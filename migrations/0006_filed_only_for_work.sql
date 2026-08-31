-- filed_task belongs to work and to nothing else.
--
-- 0005 put the two columns in without tying them together, which left a row
-- able to say it was filed on a task while calling its prevention a rule. The
-- ledger is append-only, so a contradictory row written by a future bug or by
-- a writer outside this codebase could not be corrected afterwards — which is
-- the argument for the constraint rather than a convention.
ALTER TABLE ledger
    ADD CONSTRAINT ledger_filed_only_for_work
        CHECK (filed_task IS NULL OR prevention_kind = 'work');
