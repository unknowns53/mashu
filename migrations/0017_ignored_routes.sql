-- ---------------------------------------------------------------------------
-- 0017: a route to nothing (16.3, 30 段 D).
--
-- "Hold rather than guess" needs a third answer. With only "this scope" and
-- "no route", one session run from a downloads folder leaves a held run that
-- nothing can clear, and the health line — which rides into every session
-- opening by design — stays red forever. A warning that is always on is a
-- warning nobody reads, which un-builds the reason health was put there.
--
-- So a route may point at no scope, and that means: seen, and deliberately not
-- captured.
-- ---------------------------------------------------------------------------

ALTER TABLE scope_route ALTER COLUMN scope_id DROP NOT NULL;
