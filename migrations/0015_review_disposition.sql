-- ---------------------------------------------------------------------------
-- 0015: making a deferral say so (18.1, 30 段 C).
--
-- Skipping an item in a bundle used to leave it pending, which is the same row
-- state as never having been looked at. The reviewer then meets it again at the
-- top of the queue on the next sitting, reads it again, and skips it again;
-- and the queue cannot distinguish "nobody has seen this" from "somebody looked
-- and chose to wait", which are the two things a review load measurement most
-- needs to tell apart.
--
-- So a deferral is a decision with a reason, recorded like the other two.
-- ---------------------------------------------------------------------------

ALTER TABLE proposal ADD COLUMN deferred_at  TIMESTAMPTZ;
ALTER TABLE proposal ADD COLUMN review_note  TEXT;

CREATE INDEX idx_proposal_deferred ON proposal (deferred_at)
    WHERE status = 'pending' AND deferred_at IS NOT NULL;
