"""Which scope a working directory belongs to (16.3).

The worker does not infer this. A scope guessed wrong is not a near miss: the
knowledge lands somewhere the sessions that need it will never look, and
nothing about the result reads as wrong, so the mistake is only found by
missing what should have been remembered. The map is stated by a person, and a
directory nobody mapped produces a held run and a warning rather than a guess.

Longest prefix wins, so a subdirectory can be split off from the tree around it
without restating the tree.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any
from uuid import UUID

import psycopg

from mashu.errors import MashuError


class RouteError(MashuError):
    """A route cannot be written as asked."""


def normalise(path: str) -> str:
    """One spelling per directory, so two routes cannot both look like the match."""
    expanded = os.path.expanduser(str(path).strip())
    return str(pathlib.PurePosixPath(expanded)).rstrip("/") or "/"


def add(
    cur: psycopg.Cursor, *, path_prefix: str, scope_id: UUID | None, created_by: str
) -> dict[str, Any]:
    """Map a directory tree onto a scope, replacing any earlier mapping for it.

    A route to nothing is an answer too. Without one, the only reply to "this
    directory is not worth capturing" is a held run that never clears, and a
    health warning that is permanently on is a health warning nobody reads.
    """
    prefix = normalise(path_prefix)
    cur.execute(
        """
        INSERT INTO scope_route (path_prefix, scope_id, created_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (path_prefix)
        DO UPDATE SET scope_id = EXCLUDED.scope_id, created_by = EXCLUDED.created_by
        RETURNING *
        """,
        (prefix, scope_id, created_by),
    )
    return cur.fetchone()


def remove(cur: psycopg.Cursor, *, path_prefix: str) -> bool:
    cur.execute("DELETE FROM scope_route WHERE path_prefix = %s", (normalise(path_prefix),))
    return cur.rowcount > 0


def all_routes(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT r.route_id, r.path_prefix, r.scope_id, r.created_by, s.name AS scope_name
        FROM scope_route r LEFT JOIN scope s ON s.scope_id = r.scope_id
        ORDER BY length(r.path_prefix) DESC, r.path_prefix
        """
    )
    return cur.fetchall()


def resolve(cur: psycopg.Cursor, cwd: str | None) -> tuple[UUID | None, bool]:
    """The scope a directory maps to, and whether it maps to nothing on purpose.

    Three answers, not two: this scope, deliberately no scope, and no route at
    all. Only the third is a gap for a person to fill; collapsing it with the
    second is what turns "hold and warn" into a warning that never goes out.

    Matching is on whole path segments. A plain string prefix would let
    /a/mashu-old answer for the route /a/mashu, which is a different project
    with a name that happens to start the same way.
    """
    if not cwd:
        return None, False
    here = normalise(cwd)
    for row in all_routes(cur):
        prefix = row["path_prefix"]
        if here == prefix or here.startswith(prefix.rstrip("/") + "/"):
            return row["scope_id"], row["scope_id"] is None
    return None, False
