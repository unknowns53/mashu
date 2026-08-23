-- Bootstrap pushed every active preference and every current state, so its
-- cost tracked the size of the store. Measured against the real migration it
-- wanted 45,840 token against a 2,000 ceiling, and trimming to fit turned
-- almost every preference into a bare title. Raising the ceiling would not
-- help: what is wrong is that a fixed cost was defined as a function of
-- inventory.
--
-- Two columns separate what a memory is from how it is delivered.
--
-- delivery says where it goes. startup_required is pushed at session start,
-- scope_required once the session knows its scope, pull_only only when asked
-- for. Nothing is pushed unless someone marked it, so the fixed cost stops
-- growing with the store. It sits on the entity because it is a fact about
-- the concept's role, not about any one wording of it, and only the user sets
-- it: a delivery an agent could set for itself is a way to write standing
-- instructions by another route.
--
-- directive is the short standing form of the same knowledge, and content
-- keeps the whole of it. The push carries the directive and the pull carries
-- the content, so shortening no longer means losing the reasons. It is a
-- column rather than a runtime summary because a summary made at read time is
-- an interpretation nobody reviewed, and this system does not store those
-- unreviewed.

ALTER TABLE memory_entity
    ADD COLUMN delivery TEXT NOT NULL DEFAULT 'pull_only'
        CHECK (delivery IN ('startup_required', 'scope_required', 'pull_only'));

ALTER TABLE memory_version ADD COLUMN directive TEXT;

CREATE INDEX idx_entity_delivery ON memory_entity (delivery)
    WHERE delivery <> 'pull_only';
