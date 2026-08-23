-- A scope with nothing adopted yet is not a scope that knows nothing; it is a
-- scope that is not ready to answer. Those two look identical from outside,
-- and the migration made the difference matter: sixty-three memories landed in
-- two scopes and retrieval returned nothing, which reads to an agent as "there
-- is nothing here" rather than "this is not open yet".
--
-- lifecycle says which it is. It does not loosen retrieval — a seeding scope
-- answers exactly as it does now, because it genuinely has nothing settled to
-- answer with. What it changes is that the state is declared rather than
-- inferred, so the session bootstrap can put it on the map and say so.
--
-- readiness is what promotion is checked against. It is a manifest rather than
-- a fixed count of state, preference and decision, because a fixed count makes
-- a scope with no decisions in it invent one to get out of seeding. Each type
-- is either required or explicitly declared unnecessary, and the declaration
-- is the user's to make.

ALTER TABLE scope
    ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'seeding'
        CHECK (lifecycle IN ('seeding', 'operational')),
    ADD COLUMN readiness JSONB NOT NULL DEFAULT
        '{"state": "required", "preference": "required", "decision": "required"}'::jsonb;

-- A scope that is already answering is already operational; saying otherwise
-- would retire a working scope on the way past.
UPDATE scope s SET lifecycle = 'operational'
WHERE EXISTS (
    SELECT 1 FROM memory_entity e
    WHERE e.scope_id = s.scope_id AND e.active_version IS NOT NULL
);
