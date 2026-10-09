"""Manage memory scopes and their capacity totals."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, events, redact
from mashu.errors import MashuError


def create_scope(
    cur: psycopg.Cursor, *, name: str, summary: str | None = None, actor: str
) -> dict[str, Any]:
    """Open a scope, refusing a name that is already taken."""
    verdict = redact.gate({"name": name, "summary": summary})
    if get_scope(cur, name) is not None:
        raise MashuError(f"scope '{name}' already exists")
    cur.execute(
        "INSERT INTO scope (name, summary, created_by) VALUES (%s, %s, %s) RETURNING *",
        (name, summary, actor),
    )
    row = cur.fetchone()
    events.record(cur, "scope_created", actor, detail={"name": name})
    row["unchecked"] = verdict.unchecked
    if verdict.malformed:
        row["malformed"] = verdict.malformed
    return row


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


def get_scope(cur: psycopg.Cursor, name: str) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM scope WHERE name = %s", (name,))
    return cur.fetchone()


def require_scope(cur: psycopg.Cursor, name: str) -> dict[str, Any]:
    """The scope by that name, or an error that says which names exist."""
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
        SELECT scope_id, count(*) AS n FROM memory
        WHERE status = 'active' AND scope_id IS NOT NULL
        GROUP BY scope_id
        """
    )
    active = {row["scope_id"]: row["n"] for row in cur.fetchall()}
    pushed = capacity.bootstrap_totals(cur)["scopes"]

    for row in rows:
        row["n_active"] = active.get(row["scope_id"], 0)
        row["push_tokens"] = pushed.get(row["scope_id"], 0)
    return rows
