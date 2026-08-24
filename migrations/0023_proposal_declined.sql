-- ---------------------------------------------------------------------------
-- 0023: stop one word from meaning two things (specification 11, 15).
--
-- `rejected` was a status on both a proposal and a version, and the two are
-- not the same claim. On a version it is a reading the layer 3 hands back
-- beside disproven and dormant: a reviewer looked at this and did not take it,
-- do not derive it again. On a proposal it is the record of a procedure that
-- ended. One is knowledge about content, the other is bookkeeping about an
-- act, and a person reading `rejected` had to know which table they were
-- looking at before they knew what it meant.
--
-- The proposal side is the one renamed. The version side is agent-facing — it
-- goes out with every layer 3 answer and reads as one of three retirements —
-- while the proposal side is read by a person working through a queue, and
-- the person is who was confused.
-- ---------------------------------------------------------------------------
ALTER TABLE proposal DROP CONSTRAINT IF EXISTS decision_is_complete;
ALTER TABLE proposal DROP CONSTRAINT IF EXISTS proposal_status_check;

UPDATE proposal SET status = 'declined' WHERE status = 'rejected';

ALTER TABLE proposal ADD CONSTRAINT proposal_status_check
    CHECK (status IN ('pending', 'approved', 'declined', 'auto_committed'));

ALTER TABLE proposal ADD CONSTRAINT decision_is_complete CHECK (
    (status = 'pending'  AND reviewer IS NULL AND decided_at IS NULL)
    OR (status = 'declined' AND decided_at IS NOT NULL
                            AND decision_reason IS NOT NULL)
    OR (status IN ('approved', 'auto_committed') AND decided_at IS NOT NULL)
);

-- The event that records the act is renamed with it, for the same reason. Old
-- rows are left alone: event_log is append only and a trigger enforces it, so
-- the rename leaves a seam rather than rewriting the past. Whatever counts
-- these has to know both names until the older rows fall out of its window.
