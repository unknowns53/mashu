-- The read guarantee, moved from the session to the action (30.1).
--
-- Section 6.1 put the guarantee at session start and called its own first
-- stage a pseudo-push depending on the agent's obedience. The observed
-- failure was neither stage: the opening fired, the index was delivered, and
-- the query never happened at the moment a decision was taken hours later.
-- What is in context and what is in front of a judgement are different things.
--
-- A row here pins one adopted memory to one kind of action. Before that action
-- runs, what is pinned is put in front of it and the action is refused once.
-- The refusal is the point: this is not a reminder to read, it is being unable
-- to proceed without having read. The same shape as the connection wrapper
-- that ended three network incidents, applied at a smaller radius.
CREATE TABLE action_guard (
    guard_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action      text NOT NULL CHECK (length(trim(action)) > 0),
    memory_id   uuid NOT NULL REFERENCES memory_entity(memory_id),
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (action, memory_id)
);

CREATE INDEX idx_action_guard_action ON action_guard (action);
