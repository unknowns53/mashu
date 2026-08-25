-- Where the sweeper got to, per root (30.1, 16.3).
--
-- The sweeper looked back a fixed number of days, and health counted rows in
-- extraction_run. Both together have a silent state: if the hook stops
-- enqueueing and the sweeper's window passes over the transcripts before it
-- next runs, the lost sessions are in neither place, so health reports zero
-- failures and capture is dead. That is the one failure a year of nobody
-- attending would not surface.
--
-- No spool is needed. The transcripts are on disk and are the spool. What was
-- missing was a memory of where the reading got to, so that a gap of any
-- length is walked rather than skipped.
CREATE TABLE sweep_watermark (
    source_cli  text PRIMARY KEY,
    swept_to    timestamptz NOT NULL,
    swept_at    timestamptz NOT NULL DEFAULT now(),
    files_seen  integer NOT NULL DEFAULT 0
);
