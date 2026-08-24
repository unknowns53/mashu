-- ---------------------------------------------------------------------------
-- 0020: where a session was working, so its scratch can be found again (16.3).
--
-- Scratch was looked up by (source_cli, external_session_id) alone, and that
-- key turns out not to hold. The MCP server learns its session id from the
-- environment it was started in, and a CLI hands out a new id when a
-- conversation is compacted or resumed without restarting the server. From
-- that point the server writes under the old id while the transcript being
-- extracted carries the new one, so the lookup finds nothing and the
-- extraction falls back to reading the whole log.
--
-- Measured on this machine: four live servers held the ids e73a85f5, 0ac4b8f6,
-- 930b105f and 65eecb5a while the conversation actually being served had
-- become d940f18b, and no row existed for it. The transcript carries no link
-- back to the id it continues, so the two cannot be joined by name.
--
-- They can be joined by place and time. A session was in a directory, and its
-- scratch items carry the moment they were written; a transcript names the
-- same directory and spans a stretch of clock. That is enough, and unlike the
-- id it is not something a CLI can rotate underneath us.
--
-- The failure this leaves is two sessions in one directory at the same time,
-- which will each see the other's notes. That costs a few extra turns read and
-- a stray line in the prompt, against a lookup that silently misses every
-- compacted session — and the long sessions are the compacted ones.
-- ---------------------------------------------------------------------------

ALTER TABLE agent_session ADD COLUMN cwd TEXT;

-- Answering "which sessions were working here" is the whole point of the
-- column, and it is asked once per extraction.
CREATE INDEX idx_session_cwd ON agent_session (cwd);
