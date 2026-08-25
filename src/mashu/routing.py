"""Which scope a working directory belongs to.

Nothing here infers. A directory nobody has mapped resolves to "no route",
which is a different answer from "mapped to nothing on purpose", and the
difference matters: only the first is a gap for a person to fill. Collapsing
them would turn a prompt to write one route into a warning that never stops.

Longest prefix wins, so a subdirectory can be split off from the tree around
it without restating the tree.
"""

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
    return str(pathlib.PurePosixPath(expanded)).rstrip("/") or "/"


def add_route(
    cur: psycopg.Cursor, *, path_prefix: str, scope_id: UUID | None, actor: str
) -> dict[str, Any]:
    """Map a directory tree onto a scope, replacing any earlier mapping for it.

    A route to nothing is an answer too, and the upsert is what keeps one
    directory from carrying two contradictory answers at once.
    """
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


def remove_route(cur: psycopg.Cursor, *, path_prefix: str) -> bool:
    prefix = normalise(path_prefix)
    cur.execute("DELETE FROM route WHERE path_prefix = %s RETURNING created_by", (prefix,))
    row = cur.fetchone()
    if row is None:
        return False
    events.record(cur, "route_removed", row["created_by"], detail={"path_prefix": prefix})
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
    """The scope a directory maps to, and whether it was mapped at all.

    Matching is on whole path segments. A plain string prefix would let
    /a/project-old answer for the route /a/project, which is a different tree
    with a name that happens to start the same way.
    """
    if not cwd:
        return None, False
    here = normalise(cwd)
    for row in all_routes(cur):
        prefix = row["path_prefix"]
        if here == prefix or here.startswith(prefix.rstrip("/") + "/"):
            return row["scope_id"], True
    return None, False
