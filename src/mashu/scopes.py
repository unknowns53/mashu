"""The scope ledger.

A scope is a place a rule belongs to, and only a person creates one. The
consequence of guessing is not a near miss: a memory filed under a scope the
sessions that need it never open is indistinguishable from a memory that was
never written, and nothing about the result reads as wrong.

The counts this module reports exist for the seat check (specification 5.2).
`push_tokens` is what a session opening in the scope pays for that scope
alone; `n_active` is everything homed there, guard rows included, because a
guard row occupies a person's attention even though it costs the opening
nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg

from mashu import events
from mashu.errors import MashuError
from mashu.tokens import pushed_cost


def create_scope(
    cur: psycopg.Cursor, *, name: str, summary: str | None = None, actor: str
) -> dict[str, Any]:
    """Open a scope, refusing a name that is already taken.

    Two scopes with the same name would split one body of knowledge in half
    silently, so the collision is an error rather than a merge.
    """
    if get_scope(cur, name) is not None:
        raise MashuError(f"scope '{name}' already exists")
    cur.execute(
        "INSERT INTO scope (name, summary, created_by) VALUES (%s, %s, %s) RETURNING *",
        (name, summary, actor),
    )
    row = cur.fetchone()
    events.record(cur, "scope_created", actor, detail={"name": name})
    return row


def get_scope(cur: psycopg.Cursor, name: str) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM scope WHERE name = %s", (name,))
    return cur.fetchone()


def require_scope(cur: psycopg.Cursor, name: str) -> dict[str, Any]:
    """The scope by that name, or an error that says which names exist.

    A caller who mistyped a scope cannot see the ledger from where they are
    standing, so the refusal carries it.
    """
    row = get_scope(cur, name)
    if row is not None:
        return row
    cur.execute("SELECT name FROM scope ORDER BY name")
    known = [r["name"] for r in cur.fetchall()]
    listed = ", ".join(known) if known else "none yet"
    raise MashuError(f"no scope named '{name}' (existing scopes: {listed})")


def list_scopes(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Every scope with what it currently holds and what it costs to open."""
    cur.execute("SELECT * FROM scope ORDER BY name")
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT scope_id, delivery, content FROM memory
        WHERE status = 'active' AND scope_id IS NOT NULL
        """
    )
    active: dict[Any, int] = {}
    pushed: dict[Any, list[str]] = {}
    for row in cur.fetchall():
        active[row["scope_id"]] = active.get(row["scope_id"], 0) + 1
        if row["delivery"] == "scope":
            pushed.setdefault(row["scope_id"], []).append(row["content"])

    for row in rows:
        row["n_active"] = active.get(row["scope_id"], 0)
        row["push_tokens"] = pushed_cost(pushed.get(row["scope_id"], []))
    return rows
