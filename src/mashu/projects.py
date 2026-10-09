"""Manage projects that own task state."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, redact
from mashu.errors import MashuError, RefusedError, UnknownProjectError


def create_project(
    cur: psycopg.Cursor, *, name: str, actor: str, scope_id: UUID | None = None
) -> dict[str, Any]:
    """Open a project, refusing a name already taken."""
    if not name or not name.strip():
        raise MashuError("a project needs a name: it is what tasks are filed under")
    redact.gate({"name": name})
    if get_project(cur, name) is not None:
        raise MashuError(f"project '{name}' already exists")

    cur.execute(
        "INSERT INTO project (name, scope_id) VALUES (%s, %s) RETURNING *",
        (name, scope_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "project_created",
        actor,
        detail={"project_id": str(row["project_id"]), "name": name},
    )
    return row


def update_project(
    cur: psycopg.Cursor,
    project_id: UUID,
    *,
    name: str,
    scope_id: UUID | None,
    actor: str,
) -> dict[str, Any]:
    """Edit a project's label and delivery scope, retaining all of its tasks."""
    # Local import avoids making the projects/tasks relationship circular at import time.
    from mashu import tasks

    name = (name or "").strip()
    if not name:
        raise MashuError("a project needs a name: it is what tasks are filed under")
    redact.gate({"name": name})

    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, tasks.LOCK_PROJECT_STATE),
    )
    cur.execute("SELECT * FROM project WHERE project_id = %s FOR UPDATE", (project_id,))
    current = cur.fetchone()
    if current is None:
        raise UnknownProjectError(f"no project '{project_id}'")
    cur.execute("SELECT project_id FROM project WHERE name = %s", (name,))
    taken = cur.fetchone()
    if taken is not None and taken["project_id"] != project_id:
        raise MashuError(f"project '{name}' already exists")

    before = tasks.pushed_totals(cur)["worst"]
    cur.execute(
        "UPDATE project SET name = %s, scope_id = %s WHERE project_id = %s RETURNING *",
        (name, scope_id, project_id),
    )
    row = cur.fetchone()
    after = tasks.pushed_totals(cur)["worst"]
    ceiling = config.project_capacity()
    if after > ceiling and after >= before:
        raise RefusedError(
            f"the task-card share seats {ceiling} tokens and this project edit "
            f"would take the busiest scope from {before} to {after}; shorten its active "
            "task state or move work out of that scope first"
        )
    events.record(
        cur,
        "project_updated",
        actor,
        detail={
            "project_id": str(project_id),
            "from_name": current["name"],
            "to_name": name,
            "from_scope_id": str(current["scope_id"]) if current["scope_id"] else None,
            "to_scope_id": str(scope_id) if scope_id else None,
        },
    )
    return row


def get_project(cur: psycopg.Cursor, project: UUID | str) -> dict[str, Any] | None:
    """One project, by id or by name."""
    if isinstance(project, UUID):
        cur.execute("SELECT * FROM project WHERE project_id = %s", (project,))
    else:
        cur.execute("SELECT * FROM project WHERE name = %s", (project,))
    return cur.fetchone()


def require_project(cur: psycopg.Cursor, project: UUID | str) -> dict[str, Any]:
    """The project, or an error that says which ones exist."""
    row = get_project(cur, project)
    if row is not None:
        return row
    cur.execute("SELECT name FROM project WHERE archived_at IS NULL ORDER BY name")
    known = [r["name"] for r in cur.fetchall()]
    listed = ", ".join(known) if known else "none yet"
    raise UnknownProjectError(f"no project '{project}' (open projects: {listed})")


#: What every listing counts.
_COUNTS = """
SELECT project_id,
       count(*) FILTER (WHERE status = 'open' AND now() <= active_until) AS n_active,
       count(*) FILTER (WHERE status = 'open' AND now() >  active_until) AS n_dormant,
       count(*) FILTER (WHERE status = 'closed')                         AS n_closed
FROM task GROUP BY project_id
"""


def list_projects(cur: psycopg.Cursor, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Every project with what it is currently carrying."""
    cur.execute(
        f"""
        SELECT p.*, s.name AS scope_name
        FROM project p LEFT JOIN scope s ON s.scope_id = p.scope_id
        {"" if include_archived else "WHERE p.archived_at IS NULL"}
        ORDER BY p.name
        """
    )
    rows = cur.fetchall()

    cur.execute(_COUNTS)
    counts = {row["project_id"]: row for row in cur.fetchall()}
    for row in rows:
        tally = counts.get(row["project_id"])
        for key in ("n_active", "n_dormant", "n_closed"):
            row[key] = tally[key] if tally else 0
    return rows


def show_project(cur: psycopg.Cursor, project: UUID | str) -> dict[str, Any]:
    """One project and what it is carrying, without the tasks themselves."""
    row = require_project(cur, project)
    cur.execute(f"SELECT * FROM ({_COUNTS}) c WHERE project_id = %s", (row["project_id"],))
    tally = cur.fetchone()
    for key in ("n_active", "n_dormant", "n_closed"):
        row[key] = tally[key] if tally else 0
    cur.execute("SELECT name FROM scope WHERE scope_id = %s", (row["scope_id"],))
    scope = cur.fetchone()
    row["scope_name"] = scope["name"] if scope else None
    return row
