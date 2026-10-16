-- A body with no visible character would be pushed as an empty line every session
-- (docs/mashu-v2.md 5). The services refuse one first; these hold for any other writer.
ALTER TABLE memory
    ADD CONSTRAINT memory_content_nonblank CHECK (content ~ '[^[:space:]]');
ALTER TABLE memory_revision
    ADD CONSTRAINT memory_revision_content_nonblank CHECK (content ~ '[^[:space:]]');
ALTER TABLE nomination
    ADD CONSTRAINT nomination_content_nonblank CHECK (content ~ '[^[:space:]]');
ALTER TABLE temporary_context
    ADD CONSTRAINT temporary_context_content_nonblank CHECK (content ~ '[^[:space:]]');
