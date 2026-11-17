-- A task belongs to the scope its sessions open in, and the project table goes. Kept apart, a
-- scope and a project were two names for one repository, and a scope left without a project
-- gave its sessions nowhere to file a task. task.project_id keeps its name and points at a scope.

ALTER TABLE task DROP CONSTRAINT task_project_id_fkey;

-- A project without a scope takes the scope of its own name, made when there is none. Its cards
-- no longer reach every session; they reach the sessions of that scope's folders.
INSERT INTO scope (name, created_by, archived_at)
SELECT p.name, 'migration', p.archived_at
FROM project p
WHERE p.scope_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM scope s WHERE s.name = p.name);

-- A scope that now holds an open project's tasks is in use again.
UPDATE scope s SET archived_at = NULL
FROM project p
WHERE p.scope_id IS NULL AND p.name = s.name
  AND p.archived_at IS NULL AND s.archived_at IS NOT NULL;

UPDATE task t
SET project_id = coalesce(p.scope_id, (SELECT s.scope_id FROM scope s WHERE s.name = p.name))
FROM project p
WHERE p.project_id = t.project_id;

ALTER TABLE task
    ADD CONSTRAINT task_project_id_fkey FOREIGN KEY (project_id) REFERENCES scope (scope_id);

DROP TABLE project;
