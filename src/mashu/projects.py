"""Where work state belongs (v3 specification 5.1).

A project is the home of tasks, usually one repository or one line of
research. It is not a scope: a scope decides where a rule is delivered, a
project decides which work a state belongs to, and one project starting with
one main scope is as far as this goes until something measured asks for more.

Only a person creates one. An agent that could open a project would open one
whenever it failed to find the right name, and the split would be invisible
from either half — which is the same failure `task_create` spends a trigram
match to avoid one level down.

This module knows nothing about tasks beyond counting them. Everything that
reads or writes a task lives in tasks.py, which imports this one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import events, redact
from mashu.errors import MashuError, RefusedError, UnknownProjectError


def create_project(
    cur: psycopg.Cursor, *, name: str, actor: str, scope_id: UUID | None = None
) -> dict[str, Any]:
    """Open a project, refusing a name already taken.

    Two projects under one name would divide one body of work state without
    anything reading as wrong, so the collision is an error rather than a
    merge — the same call `create_scope` makes.
    """
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


#: What every listing counts. active and dormant are the lease read against
#: now() rather than stored columns (7), so they are computed here and cannot
#: drift from what the delivery path computes for itself.
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
    """One project and what it is carrying, without the tasks themselves.

    The tasks are `tasks.task_list`'s to hand over. Keeping the join out of
    here is what keeps the import one-way, and a caller printing a project
    screen is asking for both anyway.
    """
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
    """Take a project off the working list.

    Archiving says nothing about the tasks underneath it, and deliberately
    does not close them: closing is a judgement about a piece of work and this
    is a judgement about a shelf. What stops an abandoned project's states
    from being delivered is the lease running out on each of them (7), which
    needs no help from here.
    """
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
