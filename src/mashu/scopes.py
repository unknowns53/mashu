"""Manage scopes: what a repository's sessions are told, and where its tasks are filed."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, events, redact, routing
from mashu.errors import MashuError


def create_scope(
    cur: psycopg.Cursor,
    *,
    name: str,
    summary: str | None = None,
    actor: str,
    path: str | None = None,
) -> dict[str, Any]:
    """Open a scope, refusing a name that is already taken.

    `path` routes a folder to the new scope in the same transaction, so a repository is linked
    in one step and its sessions can file tasks from the start.
    """
    name = (name or "").strip()
    if not name:
        raise MashuError("a scope needs a name")
    verdict = redact.gate({"name": name, "summary": summary})
    taken = get_scope(cur, name)
    if taken is not None:
        archived = " as an archived scope" if taken.get("archived_at") else ""
        raise MashuError(f"scope '{name}' already exists{archived}")
    cur.execute(
        "INSERT INTO scope (name, summary, created_by) VALUES (%s, %s, %s) RETURNING *",
        (name, summary, actor),
    )
    row = cur.fetchone()
    events.record(cur, "scope_created", actor, detail={"name": name})
    if path:
        row["path_prefix"] = add_folder(cur, row, path, actor=actor)["path_prefix"]
    row["unchecked"] = verdict.unchecked
    if verdict.malformed:
        row["malformed"] = verdict.malformed
    return row


def add_folder(
    cur: psycopg.Cursor, scope: dict[str, Any], path: str, *, actor: str
) -> dict[str, Any]:
    """Route a folder to this scope, refusing one another scope or the ignore list holds."""
    prefix = routing.normalise(path)
    cur.execute(
        "SELECT s.name FROM route r LEFT JOIN scope s ON s.scope_id = r.scope_id "
        "WHERE r.path_prefix = %s AND r.scope_id IS DISTINCT FROM %s",
        (prefix, scope["scope_id"]),
    )
    taken = cur.fetchone()
    if taken is not None:
        owner = f"scope '{taken['name']}'" if taken["name"] else "the ignored folders"
        raise MashuError(f"{prefix} already belongs to {owner}; remove it there first")
    return routing.add_route(cur, path_prefix=prefix, scope_id=scope["scope_id"], actor=actor)


def update_scope(
    cur: psycopg.Cursor,
    scope_id: UUID,
    *,
    name: str,
    summary: str | None,
    actor: str,
) -> dict[str, Any]:
    """Edit the human-owned name and description without changing references."""
    name = (name or "").strip()
    summary = summary.strip() if summary and summary.strip() else None
    if not name:
        raise MashuError("a scope needs a name")
    redact.gate({"name": name, "summary": summary})

    cur.execute("SELECT * FROM scope WHERE scope_id = %s FOR UPDATE", (scope_id,))
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no scope {scope_id}")
    cur.execute("SELECT scope_id FROM scope WHERE name = %s", (name,))
    taken = cur.fetchone()
    if taken is not None and taken["scope_id"] != scope_id:
        raise MashuError(f"scope '{name}' already exists")

    cur.execute(
        "UPDATE scope SET name = %s, summary = %s WHERE scope_id = %s RETURNING *",
        (name, summary, scope_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "scope_updated",
        actor,
        detail={
            "scope_id": str(scope_id),
            "from_name": current["name"],
            "to_name": name,
        },
    )
    return row


def get_scope(cur: psycopg.Cursor, name: UUID | str) -> dict[str, Any] | None:
    """One scope, by id or by name; MCP hands an id over as a string."""
    try:
        scope_id = name if isinstance(name, UUID) else UUID(str(name))
    except ValueError:
        cur.execute("SELECT * FROM scope WHERE name = %s", (name,))
    else:
        cur.execute("SELECT * FROM scope WHERE scope_id = %s", (scope_id,))
    return cur.fetchone()


def require_scope(
    cur: psycopg.Cursor, name: UUID | str, *, also: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The scope by that id or name, or an error that says which names exist.

    `also` names what the caller accepts beside scopes, listed first among the valid names.
    """
    row = get_scope(cur, name)
    if row is not None and not row.get("archived_at"):
        return row
    if row is not None:
        raise MashuError(f"scope '{name}' is archived; past records name it, so it stays unused")
    # SELECT * rather than naming archived_at, so a store without that column still answers.
    cur.execute("SELECT * FROM scope ORDER BY name")
    known = [*also, *(r["name"] for r in cur.fetchall() if not r.get("archived_at"))]
    listed = f"{'valid names' if also else 'existing scopes'}: {', '.join(known) or 'none yet'}"
    cur.execute("SELECT 1 FROM topic WHERE name = %s AND archived_at IS NULL", (name,))
    if cur.fetchone() is not None:
        raise MashuError(
            f"'{name}' is a topic, not a scope; read its rules with memory_list(topic='{name}'), "
            f"and pass a scope name or nothing as scope ({listed})"
        )
    raise MashuError(f"no scope named '{name}' ({listed})")


#: What keeps a scope in use, with the words a refusal names it by.
_HOLDS = (
    (
        "active memories",
        "SELECT count(*) FROM memory WHERE status = 'active' AND (scope_id = %(id)s OR "
        "memory_id IN (SELECT memory_id FROM memory_scope WHERE scope_id = %(id)s))",
    ),
    ("routes", "SELECT count(*) FROM route WHERE scope_id = %(id)s"),
    ("open tasks", "SELECT count(*) FROM task WHERE project_id = %(id)s AND status = 'open'"),
    ("open topics", "SELECT count(*) FROM topic WHERE scope_id = %(id)s AND archived_at IS NULL"),
    (
        "temporary contexts",
        "SELECT count(*) FROM temporary_context WHERE scope_id = %(id)s AND expires_at > now()",
    ),
    (
        "pending candidates",
        "SELECT count(*) FROM nomination WHERE scope_id = %(id)s AND status = 'pending'",
    ),
    (
        "pending memory changes",
        "SELECT count(*) FROM memory_change "
        "WHERE successor_scope_id = %(id)s AND status = 'pending'",
    ),
)

#: Every table whose rows name a scope by scope_id; any such row makes removal an archive.
_NAMED_BY = (
    "ledger",
    "trace",
    "memory",
    "memory_scope",
    "nomination",
    "temporary_context",
    "topic",
)


def remove_scope(cur: psycopg.Cursor, scope_id: UUID, *, actor: str) -> dict[str, Any]:
    """Delete a scope nothing has named, or archive one that past records name.

    A scope still in use is refused with what holds it, so nothing is left delivered nowhere.
    """
    capacity.lock(cur)
    cur.execute("SELECT * FROM scope WHERE scope_id = %s FOR UPDATE", (scope_id,))
    row = cur.fetchone()
    if row is None or row.get("archived_at"):
        raise MashuError(f"no scope {scope_id}")
    held = []
    for label, sql in _HOLDS:
        cur.execute(sql, {"id": scope_id})
        n = cur.fetchone()["count"]
        if n:
            held.append(f"{n} {label}")
    if held:
        raise MashuError(
            f"scope '{row['name']}' still has {', '.join(held)}; move, retire, or remove them first"
        )
    named = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} WHERE scope_id = %(id)s)" for table in _NAMED_BY
    )
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM memory_change WHERE successor_scope_id = %(id)s) OR "
        "EXISTS (SELECT 1 FROM task WHERE project_id = %(id)s) OR " + named + " AS named",
        {"id": scope_id},
    )
    detail = {"scope_id": str(scope_id), "name": row["name"]}
    if cur.fetchone()["named"]:
        cur.execute(
            "UPDATE scope SET archived_at = now() WHERE scope_id = %s RETURNING *", (scope_id,)
        )
        archived = cur.fetchone()
        events.record(cur, "scope_archived", actor, detail=detail)
        return {**archived, "removed": "archived"}
    cur.execute("DELETE FROM scope WHERE scope_id = %s RETURNING *", (scope_id,))
    deleted = cur.fetchone()
    events.record(cur, "scope_deleted", actor, detail=detail)
    return {**deleted, "removed": "deleted"}


def list_scopes(cur: psycopg.Cursor, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Every scope not archived, with what it currently holds and what it costs to open."""
    cur.execute("SELECT * FROM scope ORDER BY name")
    rows = [row for row in cur.fetchall() if include_archived or not row.get("archived_at")]

    cur.execute(
        """
        SELECT scope_id, count(DISTINCT memory_id) AS n FROM (
            SELECT scope_id, memory_id FROM memory
            WHERE status = 'active' AND scope_id IS NOT NULL
            UNION ALL
            SELECT ms.scope_id, ms.memory_id FROM memory_scope ms
            JOIN memory m ON m.memory_id = ms.memory_id
            WHERE m.status = 'active'
        ) reached
        GROUP BY scope_id
        """
    )
    active = {row["scope_id"]: row["n"] for row in cur.fetchall()}
    pushed = capacity.bootstrap_totals(cur)["scopes"]
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
    cur.execute("SELECT scope_id, path_prefix FROM route WHERE scope_id IS NOT NULL ORDER BY 2")
    folders: dict[UUID, list[str]] = {}
    for route in cur.fetchall():
        folders.setdefault(route["scope_id"], []).append(route["path_prefix"])

    for row in rows:
        row["n_active"] = active.get(row["scope_id"], 0)
        row["push_tokens"] = pushed.get(row["scope_id"], 0)
        tally = filed.get(row["scope_id"], {})
        for key in ("n_tasks_active", "n_tasks_dormant", "n_tasks_closed"):
            row[key] = tally.get(key, 0)
        row["folders"] = folders.get(row["scope_id"], [])
    return rows
