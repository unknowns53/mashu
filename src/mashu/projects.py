"""Manage projects that own task state."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import events, redact
from mashu.errors import MashuError, RefusedError, UnknownProjectError


def create_project(
    cur: psycopg.Cursor, *, name: str, actor: str, scope_id: UUID | None = None
) -> dict[str, Any]:
    """Open a project, refusing a name already taken."""
    if not name or not name.strip():
        raise MashuError("a project needs a name: it is what tasks are filed under")
    verdict = redact.check(name)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
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


def archive_project(cur: psycopg.Cursor, project: UUID | str, *, actor: str) -> dict[str, Any]:
    """Take a project off the working list."""
    row = require_project(cur, project)
    if row["archived_at"] is not None:
        raise MashuError(f"project '{row['name']}' was already archived")
    cur.execute(
        "UPDATE project SET archived_at = now() WHERE project_id = %s RETURNING *",
        (row["project_id"],),
    )
    archived = cur.fetchone()
    events.record(
        cur,
        "project_archived",
        actor,
        detail={"project_id": str(row["project_id"]), "name": row["name"]},
    )
    return archived
