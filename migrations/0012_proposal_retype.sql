-- ---------------------------------------------------------------------------
-- 0012: allow a proposal to ask for an entity's type to be corrected
-- (specification 8, v0.11).
--
-- The type was immutable because no operation moved it, not because anything
-- required it to hold. Correcting one meant creating a second entity with the
-- same content and merging: three operations and a new version for a change
-- that alters no content at all.
--
-- It stays on the human review line. The type decides which commit line the
-- entity's later changes take (section 17), so an agent that could set it
-- could choose its own route past review.
-- ---------------------------------------------------------------------------
ALTER TABLE proposal DROP CONSTRAINT IF EXISTS proposal_operation_check;
ALTER TABLE proposal ADD CONSTRAINT proposal_operation_check
    CHECK (operation IN ('create', 'update_version', 'change_status',
                         'restore', 'merge', 'retype'));
