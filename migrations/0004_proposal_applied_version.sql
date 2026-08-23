-- Specification 15 and 17.
--
-- A candidate commit writes its version at propose time: that is what gives
-- layer 2 of retrieval something to show while the review is outstanding
-- (21.1). Approving it later only moves the active pointer, so the proposal
-- has to remember which version it wrote.

ALTER TABLE proposal
    ADD COLUMN applied_version UUID REFERENCES memory_version(version_id);
