-- Bundle ordering (18.1) sorts by grounds-before-conclusions and then by when
-- the proposal was made. The second key was created_at, which is now(), which
-- is transaction start time: every proposal written in one transaction shares
-- it exactly, leaving the order within a rank undefined.
--
-- That is not a rare corner. The migration of 27.1 writes a whole file of
-- proposals in one transaction, and it is precisely the imported bundles whose
-- reading order the user depends on.
--
-- A monotonic column gives the tiebreak an answer, and the answer it gives is
-- the order the proposals were actually made in.

ALTER TABLE proposal ADD COLUMN seq BIGSERIAL;
CREATE INDEX idx_proposal_seq ON proposal (seq);
