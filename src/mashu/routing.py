"""Resolve a working directory to its configured scope."""

from __future__ import annotations

import os
import pathlib
from typing import Any
from uuid import UUID

import psycopg

from mashu import events


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
