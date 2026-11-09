-- A person retires a rule with a reason and no kind to choose. "Wrong" and "no longer
-- needed" become one kind, withdrawn, and a similar rule coming back shows its reason and
-- waits for confirmation, as invalidated did (docs/memory.md 4.3). superseded and relocated
-- stay, recorded by the operations that link a successor or a Temporary Context.
ALTER TABLE memory DROP CONSTRAINT memory_retirement_kind_check;
ALTER TABLE memory_change DROP CONSTRAINT memory_change_retirement_kind_check;

UPDATE memory SET retirement_kind = 'withdrawn'
WHERE retirement_kind IN ('invalidated', 'out_of_scope');
UPDATE memory_change SET retirement_kind = 'withdrawn'
WHERE retirement_kind IN ('invalidated', 'out_of_scope');

ALTER TABLE memory ADD CONSTRAINT memory_retirement_kind_check
    CHECK (retirement_kind IS NULL OR retirement_kind IN
           ('withdrawn', 'superseded', 'relocated', 'legacy'));
ALTER TABLE memory_change ADD CONSTRAINT memory_change_retirement_kind_check
    CHECK (retirement_kind IN ('withdrawn', 'superseded', 'relocated', 'legacy'));
