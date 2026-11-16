"""Resolve a working directory to its configured scope, and a scope to what it includes."""

from __future__ import annotations

import os
import pathlib
from typing import Any
from uuid import UUID

import psycopg

from mashu import events
from mashu.errors import MashuError


def normalise(path: str) -> str:
    """One spelling per directory, so two routes cannot both look like the match."""
    expanded = os.path.expanduser(str(path).strip())
    if os.name == "nt":
        return pathlib.PureWindowsPath(expanded).as_posix().rstrip("/").lower() or "/"
    return str(pathlib.PurePosixPath(expanded)).rstrip("/") or "/"


def add_route(
    cur: psycopg.Cursor, *, path_prefix: str, scope_id: UUID | None, actor: str
) -> dict[str, Any]:
    """Map a directory tree onto a scope, replacing any earlier mapping for it."""
    if not str(path_prefix).strip():
        raise MashuError("a route needs the directory it maps")
    prefix = normalise(path_prefix)
    cur.execute(
        """
        INSERT INTO route (path_prefix, scope_id, created_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (path_prefix)
        DO UPDATE SET scope_id = EXCLUDED.scope_id, created_by = EXCLUDED.created_by
        RETURNING *
        """,
        (prefix, scope_id, actor),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "route_set",
        actor,
        detail={"path_prefix": prefix, "scope_id": str(scope_id) if scope_id else None},
    )
    return row


def remove_route(cur: psycopg.Cursor, *, path_prefix: str, actor: str) -> bool:
    """Unmap a directory, recording who unmapped it."""
    prefix = normalise(path_prefix)
    cur.execute("DELETE FROM route WHERE path_prefix = %s", (prefix,))
    if cur.rowcount == 0:
        return False
    events.record(cur, "route_removed", actor, detail={"path_prefix": prefix})
    return True


def update_route(
    cur: psycopg.Cursor,
    route_id: UUID,
    *,
    path_prefix: str,
    scope_id: UUID | None,
    actor: str,
) -> dict[str, Any]:
    """Edit a selected route atomically, preserving its identity."""
    prefix = normalise(path_prefix)
    cur.execute("SELECT * FROM route WHERE route_id = %s FOR UPDATE", (route_id,))
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no route {route_id}")
    cur.execute("SELECT route_id FROM route WHERE path_prefix = %s", (prefix,))
    taken = cur.fetchone()
    if taken is not None and taken["route_id"] != route_id:
        raise MashuError(f"route '{prefix}' already exists")

    cur.execute(
        """
        UPDATE route SET path_prefix = %s, scope_id = %s, created_by = %s
        WHERE route_id = %s RETURNING *
        """,
        (prefix, scope_id, actor, route_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "route_updated",
        actor,
        detail={
            "route_id": str(route_id),
            "from_path": current["path_prefix"],
            "to_path": prefix,
            "from_scope_id": str(current["scope_id"]) if current["scope_id"] else None,
            "to_scope_id": str(scope_id) if scope_id else None,
        },
    )
    return row


def all_routes(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Every route, longest prefix first, which is also resolution order."""
    cur.execute(
        """
        SELECT r.route_id, r.path_prefix, r.scope_id, r.created_by, r.created_at,
               s.name AS scope_name
        FROM route r LEFT JOIN scope s ON s.scope_id = r.scope_id
        ORDER BY length(r.path_prefix) DESC, r.path_prefix
        """
    )
    return cur.fetchall()


def resolve(cur: psycopg.Cursor, cwd: str | None) -> tuple[UUID | None, bool]:
    """The scope a directory maps to, and whether it was mapped at all."""
    if not cwd:
        return None, False
    here = normalise(cwd)
    for row in all_routes(cur):
        prefix = row["path_prefix"]
        if here == prefix or here.startswith(prefix.rstrip("/") + "/"):
            return row["scope_id"], True
    return None, False


def _include_table(cur: psycopg.Cursor) -> bool:
    # Bootstrap and guard run from hooks, so a store without the migration must still answer.
    cur.execute("SELECT to_regclass('scope_include') IS NOT NULL AS present")
    return cur.fetchone()["present"]


def openings(cur: psycopg.Cursor) -> dict[UUID, tuple[UUID, ...]]:
    """Every scope with the scopes a session routed to it receives, itself first."""
    cur.execute("SELECT scope_id FROM scope")
    seen: dict[UUID, list[UUID]] = {row["scope_id"]: [row["scope_id"]] for row in cur.fetchall()}
    if _include_table(cur):
        cur.execute(
            """
            SELECT i.scope_id, i.included_scope_id FROM scope_include i
            JOIN scope s ON s.scope_id = i.included_scope_id
            ORDER BY s.name
            """
        )
        for row in cur.fetchall():
            seen[row["scope_id"]].append(row["included_scope_id"])
    return {scope_id: tuple(ids) for scope_id, ids in seen.items()}


def visible(cur: psycopg.Cursor, scope_id: UUID | None) -> tuple[UUID, ...]:
    """The scopes whose knowledge a session in this scope receives; none when unrouted."""
    if scope_id is None:
        return ()
    if not _include_table(cur):
        return (scope_id,)
    cur.execute(
        """
        SELECT i.included_scope_id FROM scope_include i
        JOIN scope s ON s.scope_id = i.included_scope_id
        WHERE i.scope_id = %s ORDER BY s.name
        """,
        (scope_id,),
    )
    return (scope_id, *(row["included_scope_id"] for row in cur.fetchall()))
