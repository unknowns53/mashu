-- Deferrals remain pending but are hidden from the review queue (5.1).
ALTER TABLE nomination
    ADD COLUMN deferred_at  TIMESTAMPTZ,
    ADD COLUMN defer_reason TEXT;
