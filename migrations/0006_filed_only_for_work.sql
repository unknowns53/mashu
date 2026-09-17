-- Require filed_task only for work fixes.
ALTER TABLE ledger
    ADD CONSTRAINT ledger_filed_only_for_work
        CHECK (filed_task IS NULL OR prevention_kind = 'work');
