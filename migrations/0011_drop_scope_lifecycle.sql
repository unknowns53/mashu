-- ---------------------------------------------------------------------------
-- 0011: drop the scope lifecycle and its readiness manifest (specification 7.1,
-- withdrawn in v0.11).
--
-- They existed to tell a scope that knows nothing apart from one that is not
-- open yet, because both answered a query with nothing. That "nothing" was the
-- relative cap of 21.1, withdrawn in the same version: a scope with nothing
-- adopted now answers with tagged candidates, so the two states no longer look
-- alike from outside and there is nothing left to declare.
--
-- The columns are dropped rather than left unused. A label nothing maintains
-- goes stale, and a stale label is worse than none: it is read as current.
-- ---------------------------------------------------------------------------
ALTER TABLE scope DROP COLUMN IF EXISTS lifecycle;
ALTER TABLE scope DROP COLUMN IF EXISTS readiness;
