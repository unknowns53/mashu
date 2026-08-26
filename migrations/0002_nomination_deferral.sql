-- Putting a candidate off, which is not deciding it (5.1).
--
-- This is deliberately not a fourth status. A status says what a person
-- concluded, and "not now" is not a conclusion: the row is still pending, it
-- still counts against the queue `status` reports, and any of the flag paths
-- can still act on it by name. All these two columns do is take it out of the
-- way of the sitting, and say why it was put there, so the next reader meets
-- their own reason rather than a candidate that keeps reappearing unexplained.
ALTER TABLE nomination
    ADD COLUMN deferred_at  TIMESTAMPTZ,
    ADD COLUMN defer_reason TEXT;
