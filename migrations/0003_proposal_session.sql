-- Specification 18.1 and 26 (v0.7).
--
-- Review is done a session's worth at a time, because the cost of reviewing is
-- rebuilding context rather than reading proposals, and proposals from one
-- session share theirs. That grouping needs the proposal to remember where it
-- came from.

ALTER TABLE proposal
    ADD COLUMN session_id UUID REFERENCES agent_session(session_id);

-- The review queue lists pending work grouped by session, oldest first.
CREATE INDEX idx_proposal_pending_session ON proposal (session_id, created_at)
    WHERE status = 'pending';
