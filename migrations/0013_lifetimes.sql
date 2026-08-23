-- ---------------------------------------------------------------------------
-- 0013: the two stores the specification named and never built, and the ledger
-- that keeps an unattended capture from failing silently (13.1, 25.1, 25.2,
-- 16.3).
--
-- temporary_context holds what is true until a stated moment. It is not a
-- Memory: no proposal, no status machine, no active pointer. Section 10 makes
-- the pointer the single truth, so an expiring version still pointed at would
-- be Active by definition and invalid in retrieval, which is the duality that
-- section exists to prevent. A separate table disappears through a read filter
-- at the wall clock, with no worker in the way that could be down.
--
-- extraction_run is the opposite concern. Automatic capture that fails quietly
-- is worse than none, because the store looks like it is being maintained. A
-- row exists for every transcript ever seen, including the ones deliberately
-- skipped.
-- ---------------------------------------------------------------------------

CREATE TABLE temporary_context (
    context_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id          UUID REFERENCES scope(scope_id),
    kind              TEXT NOT NULL CHECK (kind IN ('fact', 'preference')),
    content           TEXT NOT NULL,
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    source_type       TEXT NOT NULL,
    source_reference  TEXT,
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at        TIMESTAMPTZ,
    revocation_reason TEXT,
    CHECK (expires_at > valid_from)
);

CREATE INDEX idx_temporary_live ON temporary_context (scope_id, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE extraction_run (
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_cli          TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    transcript_digest   TEXT NOT NULL,
    extractor_version   TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'queued'
                        CHECK (state IN ('queued', 'running', 'succeeded',
                                         'skipped', 'retrying', 'failed')),
    attempts            INT NOT NULL DEFAULT 0,
    model               TEXT,
    input_tokens        INT,
    output_tokens       INT,
    note                TEXT,
    last_error          TEXT,
    next_retry_at       TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_cli, external_session_id, transcript_digest, extractor_version)
);

CREATE INDEX idx_run_pending ON extraction_run (state, created_at)
    WHERE state IN ('queued', 'retrying');

-- The scratch column has existed since 0001 and nothing ever wrote it. What was
-- missing was the join to the outside: without a stable external id the same
-- transcript cannot be recognised twice, so an unattended re-run is not
-- idempotent.
ALTER TABLE agent_session ADD COLUMN source_cli          TEXT;
ALTER TABLE agent_session ADD COLUMN external_session_id TEXT;
ALTER TABLE agent_session ADD COLUMN transcript_digest   TEXT;
ALTER TABLE agent_session ADD COLUMN checkpoint          TEXT;

CREATE UNIQUE INDEX idx_session_external ON agent_session (source_cli, external_session_id)
    WHERE external_session_id IS NOT NULL;
