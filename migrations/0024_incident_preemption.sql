-- A cause for the failure section 6.1 did not model (30.1).
--
-- The old split asked whether the store was pushed too little or pulled too
-- rarely. Both send you back to making the index better. What was observed is
-- neither: the index was there, it was delivered, and the query never happened
-- because a static file always in context already held an answer and never
-- declares itself out of date or out of scope. The repair is to delete the
-- value from that file, which is the opposite move, so counting it as a pull
-- miss would send the evidence to the wrong question.
ALTER TABLE incident DROP CONSTRAINT incident_check;
ALTER TABLE incident ADD CONSTRAINT incident_check CHECK (
    (kind = 'missed' AND cause IN ('bootstrap', 'pull', 'source_preemption', 'capture'))
    OR (kind = 'stale' AND cause IN ('filter', 'inventory', 'marker'))
);
