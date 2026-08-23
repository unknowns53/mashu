-- Specification 21, scope detection.
--
-- Detection compared the query against every scope's name and description by
-- embedding all of them on each query. Scope names change about as often as
-- the user creates a scope, so that was the same work repeated for every
-- search, and it forced the model to be loaded even when the query vector
-- itself was already known.

ALTER TABLE scope
    ADD COLUMN name_embedding vector(1024);

CREATE INDEX idx_scope_name_embedding
    ON scope USING hnsw (name_embedding vector_cosine_ops);
