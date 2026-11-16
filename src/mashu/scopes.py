"""Manage memory scopes and their capacity totals."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, events, redact, routing, temporary
from mashu.errors import MashuError


def create_scope(
    cur: psycopg.Cursor, *, name: str, summary: str | None = None, actor: str
) -> dict[str, Any]:
    """Open a scope, refusing a name that is already taken."""
    if not name.strip():
        raise MashuError("a scope needs a name")
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


def require_scope(cur: psycopg.Cursor, name: str, *, also: tuple[str, ...] = ()) -> dict[str, Any]:
    """The scope by that name, or an error that says which names exist.

    `also` names what the caller accepts beside scopes, listed first among the valid names.
    """
    row = get_scope(cur, name)
    if row is not None:
        return row
    cur.execute("SELECT name FROM scope ORDER BY name")
    known = [*also, *(r["name"] for r in cur.fetchall())]
    listed = f"{'valid names' if also else 'existing scopes'}: {', '.join(known) or 'none yet'}"
    cur.execute("SELECT 1 FROM topic WHERE name = %s AND archived_at IS NULL", (name,))
    if cur.fetchone() is not None:
        raise MashuError(
            f"'{name}' is a topic, not a scope; read its rules with memory_list(topic='{name}'), "
            f"and pass a scope name or nothing as scope ({listed})"
        )
    raise MashuError(f"no scope named '{name}' ({listed})")


def included_names(cur: psycopg.Cursor, scope_id: UUID | None) -> list[str]:
    """The names of the scopes this one includes, for showing a session what it receives."""
    if scope_id is None:
        return []
    ids = routing.visible(cur, scope_id)[1:]
    if not ids:
        return []
    cur.execute("SELECT name FROM scope WHERE scope_id = ANY(%s) ORDER BY name", (list(ids),))
    return [row["name"] for row in cur.fetchall()]


def set_includes(
    cur: psycopg.Cursor, scope_id: UUID, included: list[UUID], *, actor: str
) -> dict[str, Any]:
    """Make this scope include exactly these scopes, refusing what would overfill an opening.

    Dropping an include only makes openings lighter, so only the added ones are weighed.
    """
    wanted = list(dict.fromkeys(included))
    if scope_id in wanted:
        raise MashuError("a scope cannot include itself")
    # Serialise with every capacity check, which reads the openings this write changes.
    capacity.lock(cur)
    current = routing.openings(cur)
    if scope_id not in current:
        raise MashuError(f"no scope {scope_id}")
    names = _names(cur)
    for other in wanted:
        if other not in current:
            raise MashuError(f"no scope {other}")
    if wanted:
        includers = [s for s, ids in current.items() if scope_id in ids[1:]]
        if includers:
            raise MashuError(
                f"scope '{names[scope_id]}' is included by "
                f"{', '.join(repr(names[s]) for s in includers)}, so it cannot include others; "
                "inclusion is one level deep"
            )
        nested = [other for other in wanted if len(current[other]) > 1]
        if nested:
            raise MashuError(
                f"{', '.join(repr(names[s]) for s in nested)} include other scopes, so "
                f"'{names[scope_id]}' cannot include them; inclusion is one level deep"
            )
    before = list(current[scope_id][1:])
    added = [other for other in wanted if other not in before]
    if added:
        after = {**current, scope_id: (scope_id, *wanted)}
        verdict = capacity.check_openings(cur, after)
        if not verdict["ok"]:
            raise MashuError(verdict["refusal"])
        temporary.check_openings(cur, after)

    cur.execute(
        "DELETE FROM scope_include WHERE scope_id = %s AND NOT (included_scope_id = ANY(%s))",
        (scope_id, wanted),
    )
    for other in added:
        cur.execute(
            "INSERT INTO scope_include (scope_id, included_scope_id, created_by) "
            "VALUES (%s, %s, %s)",
            (scope_id, other, actor),
        )
    if added or len(wanted) != len(before):
        events.record(
            cur,
            "scope_includes_set",
            actor,
            detail={
                "scope_id": str(scope_id),
                "from": [names[s] for s in before],
                "to": [names[s] for s in wanted],
            },
        )
    return {"scope": names[scope_id], "includes": sorted(names[s] for s in wanted)}


def _names(cur: psycopg.Cursor) -> dict[UUID, str]:
    cur.execute("SELECT scope_id, name FROM scope")
    return {row["scope_id"]: row["name"] for row in cur.fetchall()}


def list_scopes(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Every scope with what it holds, what it adds to a session, and what it includes."""
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
    totals = capacity.bootstrap_totals(cur)
    names = {row["scope_id"]: row["name"] for row in rows}
    shape = routing.openings(cur)

    for row in rows:
        row["n_active"] = active.get(row["scope_id"], 0)
        row["push_tokens"] = totals["own"].get(row["scope_id"], 0)
        row["opening_tokens"] = totals["scopes"].get(row["scope_id"], 0)
        row["includes"] = [names[s] for s in shape.get(row["scope_id"], ())[1:]]
        row["included_by"] = sorted(
            names[s] for s, ids in shape.items() if row["scope_id"] in ids[1:]
        )
    return rows
