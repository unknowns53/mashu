"""Manage expiring context conditions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, redact, routing, settings
from mashu.errors import MashuError, RefusedError
from mashu.tokens import pushed_cost

#: Advisory lock class, in the namespace capacity.py opened.
LOCK_TEMPORARY = 4

_TOO_LONG = (
    "a temporary context lasts at most {max} days; a claim that wants {asked} is an "
    "indefinite claim wearing an expiry. If forgetting it has cost something, report "
    "that with pain_report; if it is simply true, record it with mashu remember."
)

_UNEXPIRED = """
SELECT context_id, scope_id, content, expires_at
FROM temporary_context WHERE expires_at > now()
"""


def _standing(cur: psycopg.Cursor) -> tuple[list[dict[str, Any]], dict[Any, list[dict[str, Any]]]]:
    """The conditions still applying, in the two buckets they are weighed in."""
    always: list[dict[str, Any]] = []
    scoped: dict[Any, list[dict[str, Any]]] = {}
    cur.execute(_UNEXPIRED)
    for row in cur.fetchall():
        bucket = always if row["scope_id"] is None else scoped.setdefault(row["scope_id"], [])
        bucket.append(row)
    return always, scoped


def _cost(rows: list[dict[str, Any]]) -> int:
    return pushed_cost([row["content"] for row in rows])


def _by_opening(
    scoped: dict[Any, list[dict[str, Any]]], closure: dict[Any, tuple[Any, ...]]
) -> dict[Any, list[dict[str, Any]]]:
    """The scoped conditions each scope's session receives, its included scopes' among them."""
    shape = {**{scope_id: (scope_id,) for scope_id in scoped}, **closure}
    return {
        scope_id: [row for member in members for row in scoped.get(member, [])]
        for scope_id, members in shape.items()
    }


def pushed_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What the standing conditions cost: everywhere, per scope, and at worst."""
    always, scoped = _standing(cur)
    opened = _by_opening(scoped, routing.openings(cur))
    scopes = {scope_id: _cost(rows) for scope_id, rows in opened.items() if rows}
    always_cost = _cost(always)
    return {
        "always": always_cost,
        "scopes": scopes,
        "worst": always_cost + max(scopes.values(), default=0),
        "count": len(always) + sum(len(rows) for rows in scoped.values()),
    }


def _check_room(
    cur: psycopg.Cursor,
    *,
    content: str,
    scope_id: UUID | None,
    exclude_context_id: UUID | None = None,
) -> None:
    """Refuse a condition the share has no room for."""
    # Lock before checking capacity and hold it through the write.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))

    always, scoped = _standing(cur)
    if exclude_context_id is not None:
        always = [row for row in always if row["context_id"] != exclude_context_id]
        scoped = {
            key: [row for row in rows if row["context_id"] != exclude_context_id]
            for key, rows in scoped.items()
        }
    closure = routing.openings(cur)
    opened = _by_opening(scoped, closure)
    if scope_id is not None:
        # A condition in an included scope is pushed in every scope that includes it.
        opened = {key: rows for key, rows in opened.items() if scope_id in closure.get(key, (key,))}
    against = always + max(opened.values(), key=_cost, default=[])

    cost = pushed_cost([content])
    projected = _cost(against) + cost
    ceiling = settings.limit(cur, "temporary_capacity")
    if projected <= ceiling:
        return

    # A single oversized condition cannot become valid by waiting for expiry.
    waiting = ""
    if against:
        soonest = min(row["expires_at"] for row in against)
        waiting = f"the first of them lapses on {soonest.date().isoformat()}, or "
    raise RefusedError(
        f"the temporary share seats {ceiling} tokens and this would take it to {projected} "
        f"({cost} for this condition on top of {projected - cost} already pushed by "
        f"{len(against)} standing one(s)). Nothing retires here: {waiting}"
        "say this one shorter."
    )


def check_openings(cur: psycopg.Cursor, closure: dict[UUID, tuple[UUID, ...]]) -> None:
    """Refuse a change of included scopes that would overfill a scope's temporary share."""
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))
    always, scoped = _standing(cur)
    was = _by_opening(scoped, routing.openings(cur))
    now = _by_opening(scoped, closure)
    ceiling = settings.limit(cur, "temporary_capacity")
    for scope_id, rows in now.items():
        projected = _cost(always + rows)
        if projected > ceiling and projected > _cost(always + was.get(scope_id, [])):
            raise RefusedError(
                f"the temporary share seats {ceiling} tokens and including these scopes would "
                f"take one session to {projected}. End a temporary context first, or wait for "
                "one to lapse."
            )


def put_temporary(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    days: float,
    scope_id: UUID | None = None,
) -> dict[str, Any]:
    """Record a condition together with the moment it stops being one."""
    if not 0 < days <= config.TEMPORARY_MAX_DAYS:
        raise RefusedError(_TOO_LONG.format(max=config.TEMPORARY_MAX_DAYS, asked=f"{days:g} days"))
    if not content.strip():
        raise MashuError("temporary context cannot be empty")
    verdict = redact.gate({"content": content})

    # Pushed, so it is weighed before it is written (project-state 8).
    _check_room(cur, content=content, scope_id=scope_id)

    cur.execute(
        """
        INSERT INTO temporary_context (content, scope_id, created_by, expires_at)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s))
        RETURNING *
        """,
        (content, scope_id, actor, float(days) * 86400.0),
    )
    row = cur.fetchone()
    events.record(cur, "temporary_recorded", actor, detail={"days": days})
    report: dict[str, Any] = {**row, "unchecked": verdict.unchecked}
    if verdict.malformed:
        report["malformed"] = verdict.malformed
    return report


def revise(
    cur: psycopg.Cursor,
    context_id: UUID,
    *,
    content: str,
    scope_id: UUID | None,
    actor: str,
) -> dict[str, Any]:
    """Replace a standing condition's editable text and scope, keeping its expiry."""
    content = (content or "").strip()
    if not content:
        raise MashuError("temporary context cannot be empty")
    verdict = redact.gate({"content": content})
    cur.execute(
        "SELECT * FROM temporary_context WHERE context_id = %s AND expires_at > now() FOR UPDATE",
        (context_id,),
    )
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no standing temporary context {context_id}")

    _check_room(
        cur,
        content=content,
        scope_id=scope_id,
        exclude_context_id=context_id,
    )
    cur.execute(
        """
        UPDATE temporary_context SET content = %s, scope_id = %s
        WHERE context_id = %s RETURNING *
        """,
        (content, scope_id, context_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "temporary_revised",
        actor,
        detail={
            "context_id": str(context_id),
            "from_scope_id": str(current["scope_id"]) if current["scope_id"] else None,
            "to_scope_id": str(scope_id) if scope_id else None,
        },
    )
    return {**row, **({"unchecked": True} if verdict.unchecked else {})}


def end(cur: psycopg.Cursor, context_id: UUID, *, actor: str) -> dict[str, Any]:
    """Stop a standing condition now. The row stays, ended the way expiry ends it."""
    cur.execute(
        """
        UPDATE temporary_context SET expires_at = now()
        WHERE context_id = %s AND expires_at > now() RETURNING *
        """,
        (context_id,),
    )
    ended = cur.fetchone()
    if ended is None:
        raise MashuError(f"no standing temporary context {context_id}")
    events.record(cur, "temporary_ended", actor, detail={"context_id": str(context_id)})
    return ended


def convert_to_memory(
    cur: psycopg.Cursor,
    context_id: UUID,
    *,
    actor: str,
    acknowledged_conflicts: list[UUID] | None = None,
) -> dict[str, Any]:
    """Replace a standing condition with a durable memory in one transaction.

    `acknowledged_conflicts` passes through to memories.remember, which refuses a body that
    repeats a withdrawn or legacy Memory until the caller has read those retirements.
    """
    from mashu import memories

    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))
    cur.execute(
        "SELECT * FROM temporary_context WHERE context_id = %s AND expires_at > now() FOR UPDATE",
        (context_id,),
    )
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no standing temporary context {context_id}")
    delivery = "scope" if current["scope_id"] else "always"
    memory = memories.remember(
        cur,
        content=current["content"],
        actor=actor,
        scope_id=current["scope_id"],
        delivery=delivery,
        acknowledged_conflicts=acknowledged_conflicts,
    )
    cur.execute(
        "UPDATE temporary_context SET expires_at = now() WHERE context_id = %s RETURNING *",
        (context_id,),
    )
    ended = cur.fetchone()
    events.record(
        cur,
        "temporary_converted_to_memory",
        actor,
        memory_id=memory["memory_id"],
        detail={"context_id": str(context_id), "delivery": delivery},
    )
    return {"temporary": ended, "memory": memory}


def active_temporary(cur: psycopg.Cursor, *, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """What still applies here. Expired rows are filtered, never deleted."""
    cur.execute(
        """
        SELECT * FROM temporary_context
        WHERE expires_at > now()
          AND (scope_id IS NULL OR scope_id = ANY(%(scopes)s::uuid[]))
        ORDER BY expires_at
        """,
        {"scopes": list(routing.visible(cur, scope_id))},
    )
    return cur.fetchall()
