-- Specifications 20 and 21.
--
-- Both searches are cosine distance over the same vector space but against
-- different columns, because they ask different questions: does this concept
-- already exist (title), and does this memory answer the query (content).
--
-- HNSW rather than IVFFlat: IVFFlat has to be built against existing rows to
-- pick its lists, and this table starts empty and grows a few memories at a
-- time. HNSW needs no such training pass.

CREATE INDEX idx_entity_title_embedding
    ON memory_entity USING hnsw (title_embedding vector_cosine_ops);

CREATE INDEX idx_version_content_embedding
    ON memory_version USING hnsw (content_embedding vector_cosine_ops);
