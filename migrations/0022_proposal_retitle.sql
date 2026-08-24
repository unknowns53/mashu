-- ---------------------------------------------------------------------------
-- 0022: allow a proposal to ask for an entity's title to be corrected
-- (specification 8, 14).
--
-- The title was immutable for the same reason the type was before 0012: no
-- operation moved it. That holds badly for the one type whose content is meant
-- to move. A Current State (14) is rewritten as the work advances, and its
-- title froze at whatever the first version said — so an entity whose body had
-- been brought up to date went on announcing the state it had left behind, in
-- the session opening, above the text that was correct.
--
-- It matters more than a stale line reads. The title is what entity resolution
-- compares against (20) and what layer 3 is ranked by (21.1), so a title that
-- no longer describes the entity makes it harder to find and easier to
-- duplicate.
--
-- It stays on the human review line, for the reason 0012 gives about the type
-- and one of its own: an agent that could rewrite titles could walk an entity
-- away from the one it duplicates, and the resolution check would stop seeing
-- the collision it exists to catch.
-- ---------------------------------------------------------------------------
ALTER TABLE proposal DROP CONSTRAINT IF EXISTS proposal_operation_check;
ALTER TABLE proposal ADD CONSTRAINT proposal_operation_check
    CHECK (operation IN ('create', 'update_version', 'change_status',
                         'restore', 'merge', 'retype', 'retitle'));
