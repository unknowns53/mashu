-- Where a topic's long form lives (docs/memory.md 5): a path relative to the vault named
-- by MASHU_VAULT_ROOT, or to a repository, with an optional #heading. NULL names none.
ALTER TABLE topic
    ADD COLUMN source TEXT
        CHECK (source IS NULL OR (source = btrim(source) AND char_length(source) BETWEEN 1 AND 300));

-- The active rule a pain's reporter says was delivered and not followed (docs/memory.md 3).
ALTER TABLE ledger
    ADD COLUMN violated_memory_id UUID REFERENCES memory(memory_id);
