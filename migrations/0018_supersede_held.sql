-- ---------------------------------------------------------------------------
-- 0018: one live run per session, and a dead column removed.
--
-- A held transcript that later grew produced a second row under the new
-- digest while the held one stayed. Adding the route then released both, and
-- two runs read the same session from the same mark. The ledger's unique key
-- is (cli, session, digest, extractor version) — right for idempotency, but it
-- means a growing file is a new row by construction, so the older one has to
-- be closed rather than left standing.
--
-- agent_session.checkpoint has been superseded by extraction_run.checkpoint,
-- which is an ordinal rather than text and lives on the run that used it.
-- Nothing ever wrote the old one; leaving a column with that name invites the
-- next reader to believe it means something.
-- ---------------------------------------------------------------------------

ALTER TABLE agent_session DROP COLUMN IF EXISTS checkpoint;
