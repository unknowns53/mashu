-- A scope may include other scopes, so a session routed to it also receives what they deliver
-- (memory 5). Inclusion is one level deep: a scope that includes others cannot itself be
-- included, and an included scope cannot include others. Each session's opening is then its own
-- scope plus the scopes it names directly, and capacity can weigh it without following chains.
CREATE TABLE scope_include (
    scope_id          UUID NOT NULL REFERENCES scope(scope_id),
    included_scope_id UUID NOT NULL REFERENCES scope(scope_id),
    created_by        TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, included_scope_id),
    CHECK (scope_id <> included_scope_id)
);
CREATE INDEX scope_include_included ON scope_include (included_scope_id);

CREATE FUNCTION refuse_nested_include() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Two concurrent inserts could each pass the check below against the other's absence.
    LOCK TABLE scope_include IN SHARE ROW EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM scope_include WHERE scope_id = NEW.included_scope_id) THEN
        RAISE EXCEPTION 'scope % includes other scopes and cannot be included', NEW.included_scope_id;
    END IF;
    IF EXISTS (SELECT 1 FROM scope_include WHERE included_scope_id = NEW.scope_id) THEN
        RAISE EXCEPTION 'scope % is included by another scope and cannot include', NEW.scope_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER scope_include_one_level
    BEFORE INSERT OR UPDATE ON scope_include
    FOR EACH ROW EXECUTE FUNCTION refuse_nested_include();
