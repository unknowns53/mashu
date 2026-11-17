"""Manage projects: what a repository's sessions are told, and where its tasks are filed."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, events, redact, routing
from mashu.errors import MashuError


def create_project(
    cur: psycopg.Cursor,
    *,
    name: str,
    summary: str | None = None,
    actor: str,
    path: str | None = None,
) -> dict[str, Any]:
    """Open a project, refusing a name that is already taken.

    `path` routes a folder to the new project in the same transaction, so a repository is linked
    in one step and its sessions can file tasks from the start.
    """
    name = (name or "").strip()
    if not name:
        raise MashuError("a project needs a name")
    verdict = redact.gate({"name": name, "summary": summary})
    taken = get_project(cur, name)
    if taken is not None:
        archived = " as an archived project" if taken.get("archived_at") else ""
        raise MashuError(f"project '{name}' already exists{archived}")
    cur.execute(
        "INSERT INTO project (name, summary, created_by) VALUES (%s, %s, %s) RETURNING *",
        (name, summary, actor),
    )
    row = cur.fetchone()
    events.record(cur, "project_created", actor, detail={"name": name})
    if path:
        row["path_prefix"] = add_folder(cur, row, path, actor=actor)["path_prefix"]
    row["unchecked"] = verdict.unchecked
    if verdict.malformed:
        row["malformed"] = verdict.malformed
    return row


def add_folder(
    cur: psycopg.Cursor, project: dict[str, Any] | None, path: str, *, actor: str
) -> dict[str, Any]:
    """Route a folder to this project, or to none to ignore it.

    A folder another project or the ignore list holds is refused rather than taken over, since
    the screen adding it may not show who holds it.
    """
    if not str(path or "").strip():
        raise MashuError("a route needs the directory it maps")
    prefix = routing.normalise(path)
    project_id = project["project_id"] if project else None
    cur.execute(
        "SELECT s.name FROM route r LEFT JOIN project s ON s.project_id = r.project_id "
        "WHERE r.path_prefix = %s AND r.project_id IS DISTINCT FROM %s",
        (prefix, project_id),
    )
    taken = cur.fetchone()
    if taken is not None:
        owner = f"project '{taken['name']}'" if taken["name"] else "the ignored folders"
        raise MashuError(f"{prefix} already belongs to {owner}; remove it there first")
    return routing.add_route(cur, path_prefix=prefix, project_id=project_id, actor=actor)


def update_project(
    cur: psycopg.Cursor,
    project_id: UUID,
    *,
    name: str,
    summary: str | None,
    actor: str,
) -> dict[str, Any]:
    """Edit the human-owned name and description without changing references."""
    name = (name or "").strip()
    summary = summary.strip() if summary and summary.strip() else None
    if not name:
        raise MashuError("a project needs a name")
    redact.gate({"name": name, "summary": summary})

    cur.execute("SELECT * FROM project WHERE project_id = %s FOR UPDATE", (project_id,))
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no project {project_id}")
    cur.execute("SELECT project_id FROM project WHERE name = %s", (name,))
    taken = cur.fetchone()
    if taken is not None and taken["project_id"] != project_id:
        raise MashuError(f"project '{name}' already exists")

    cur.execute(
        "UPDATE project SET name = %s, summary = %s WHERE project_id = %s RETURNING *",
        (name, summary, project_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "project_updated",
        actor,
        detail={
            "project_id": str(project_id),
            "from_name": current["name"],
            "to_name": name,
        },
    )
    return row


def get_project(cur: psycopg.Cursor, name: UUID | str) -> dict[str, Any] | None:
    """One project, by id or by name; MCP hands an id over as a string."""
    try:
        project_id = name if isinstance(name, UUID) else UUID(str(name))
    except ValueError:
        cur.execute("SELECT * FROM project WHERE name = %s", (name,))
    else:
        cur.execute("SELECT * FROM project WHERE project_id = %s", (project_id,))
    return cur.fetchone()


def require_project(
    cur: psycopg.Cursor, name: UUID | str, *, also: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The project by that id or name, or an error that says which names exist.

    `also` names what the caller accepts beside projects, listed first among the valid names.
    """
    row = get_project(cur, name)
    if row is not None and not row.get("archived_at"):
        return row
    if row is not None:
        raise MashuError(
            f"project '{row['name']}' is archived; past records name it, so it stays unused"
        )
    # SELECT * rather than naming archived_at, so a store without that column still answers.
    cur.execute("SELECT * FROM project ORDER BY name")
    known = [*also, *(r["name"] for r in cur.fetchall() if not r.get("archived_at"))]
    listed = f"{'valid names' if also else 'existing projects'}: {', '.join(known) or 'none yet'}"
    cur.execute("SELECT 1 FROM topic WHERE name = %s AND archived_at IS NULL", (name,))
    if cur.fetchone() is not None:
        raise MashuError(
            f"'{name}' is a topic, not a project; read its rules with memory_list(topic='{name}'), "
            f"and pass a project name or nothing as project ({listed})"
        )
    raise MashuError(f"no project named '{name}' ({listed})")


#: What keeps a project in use, with the words a refusal names it by.
_HOLDS = (
    (
        "active memories",
        "SELECT count(*) FROM memory WHERE status = 'active' AND (project_id = %(id)s OR "
        "memory_id IN (SELECT memory_id FROM memory_project WHERE project_id = %(id)s))",
    ),
    ("routes", "SELECT count(*) FROM route WHERE project_id = %(id)s"),
    ("open tasks", "SELECT count(*) FROM task WHERE project_id = %(id)s AND status = 'open'"),
    ("open topics", "SELECT count(*) FROM topic WHERE project_id = %(id)s AND archived_at IS NULL"),
    (
        "temporary contexts",
        "SELECT count(*) FROM temporary_context WHERE project_id = %(id)s AND expires_at > now()",
    ),
    (
        "pending candidates",
        "SELECT count(*) FROM nomination WHERE project_id = %(id)s AND status = 'pending'",
    ),
    (
        "pending memory changes",
        "SELECT count(*) FROM memory_change "
        "WHERE successor_project_id = %(id)s AND status = 'pending'",
    ),
)

#: Every table whose rows name a project by project_id; any such row makes removal an archive.
_NAMED_BY = (
    "ledger",
    "trace",
    "memory",
    "memory_project",
    "nomination",
    "temporary_context",
    "topic",
)


def remove_project(cur: psycopg.Cursor, project_id: UUID, *, actor: str) -> dict[str, Any]:
    """Delete a project nothing has named, or archive one that past records name.

    A project still in use is refused with what holds it, so nothing is left delivered nowhere.
    """
    capacity.lock(cur)
    cur.execute("SELECT * FROM project WHERE project_id = %s FOR UPDATE", (project_id,))
    row = cur.fetchone()
    if row is None or row.get("archived_at"):
        raise MashuError(f"no project {project_id}")
    held = []
    for label, sql in _HOLDS:
        cur.execute(sql, {"id": project_id})
        n = cur.fetchone()["count"]
        if n:
            held.append(f"{n} {label}")
    if held:
        raise MashuError(
            f"project '{row['name']}' still has {', '.join(held)}; "
            "move, retire, or remove them first"
        )
    named = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} WHERE project_id = %(id)s)" for table in _NAMED_BY
    )
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM memory_change WHERE successor_project_id = %(id)s) OR "
        "EXISTS (SELECT 1 FROM task WHERE project_id = %(id)s) OR " + named + " AS named",
        {"id": project_id},
    )
    detail = {"project_id": str(project_id), "name": row["name"]}
    if cur.fetchone()["named"]:
        cur.execute(
            "UPDATE project SET archived_at = now() WHERE project_id = %s RETURNING *",
            (project_id,),
        )
        archived = cur.fetchone()
        events.record(cur, "project_archived", actor, detail=detail)
        return {**archived, "removed": "archived"}
    cur.execute("DELETE FROM project WHERE project_id = %s RETURNING *", (project_id,))
    deleted = cur.fetchone()
    events.record(cur, "project_deleted", actor, detail=detail)
    return {**deleted, "removed": "deleted"}


def list_projects(cur: psycopg.Cursor, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Every project not archived, with what it currently holds and what it costs to open."""
    cur.execute("SELECT * FROM project ORDER BY name")
    rows = [row for row in cur.fetchall() if include_archived or not row.get("archived_at")]

    cur.execute(
        """
        SELECT project_id, count(DISTINCT memory_id) AS n FROM (
            SELECT project_id, memory_id FROM memory
            WHERE status = 'active' AND project_id IS NOT NULL
            UNION ALL
            SELECT ms.project_id, ms.memory_id FROM memory_project ms
            JOIN memory m ON m.memory_id = ms.memory_id
            WHERE m.status = 'active'
        ) reached
        GROUP BY project_id
        """
    )
    active = {row["project_id"]: row["n"] for row in cur.fetchall()}
    pushed = capacity.bootstrap_totals(cur)["projects"]
    cur.execute(
        """
        SELECT project_id,
               count(*) FILTER (WHERE status = 'open' AND now() <= active_until) AS n_tasks_active,
               count(*) FILTER (WHERE status = 'open' AND now() >  active_until) AS n_tasks_dormant,
               count(*) FILTER (WHERE status = 'closed')                         AS n_tasks_closed
        FROM task GROUP BY project_id
        """
    )
    filed = {row["project_id"]: row for row in cur.fetchall()}
    cur.execute("SELECT project_id, path_prefix FROM route WHERE project_id IS NOT NULL ORDER BY 2")
    folders: dict[UUID, list[str]] = {}
    for route in cur.fetchall():
        folders.setdefault(route["project_id"], []).append(route["path_prefix"])

    for row in rows:
        row["n_rules"] = active.get(row["project_id"], 0)
        row["push_tokens"] = pushed.get(row["project_id"], 0)
        tally = filed.get(row["project_id"], {})
        for key in ("n_tasks_active", "n_tasks_dormant", "n_tasks_closed"):
            row[key] = tally.get(key, 0)
        row["folders"] = folders.get(row["project_id"], [])
    return rows
