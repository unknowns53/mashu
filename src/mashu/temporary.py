"""Manage expiring context conditions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, redact
from mashu.errors import RefusedError
from mashu.tokens import pushed_cost

#: Advisory lock class, in the namespace capacity.py opened.
LOCK_TEMPORARY = 4

_TOO_LONG = (
    "a temporary context lasts at most {max} days; a claim that wants {asked} is an "
    "indefinite claim wearing an expiry. If forgetting it has cost something, report "
    "that with pain_report; if it is simply true, record it with mashu remember."
)

_UNEXPIRED = """
SELECT scope_id, content, expires_at FROM temporary_context WHERE expires_at > now()
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


def pushed_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What the standing conditions cost: everywhere, per scope, and at worst."""
    always, scoped = _standing(cur)
    scopes = {scope_id: _cost(rows) for scope_id, rows in scoped.items()}
    always_cost = _cost(always)
    return {
        "always": always_cost,
        "scopes": scopes,
        "worst": always_cost + max(scopes.values(), default=0),
        "count": len(always) + sum(len(rows) for rows in scoped.values()),
    }


def _check_room(cur: psycopg.Cursor, *, content: str, scope_id: UUID | None) -> None:
    """Refuse a condition the share has no room for."""
    # Lock before checking capacity and hold it through the write.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))

    always, scoped = _standing(cur)
    if scope_id is None:
        heaviest = max(scoped.values(), key=_cost, default=[])
        against = always + heaviest
    else:
        against = always + scoped.get(scope_id, [])

    cost = pushed_cost([content])
    projected = _cost(against) + cost
    ceiling = config.temporary_capacity()
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
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    # Pushed, so it is weighed before it is written (v3 8).
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


def active_temporary(cur: psycopg.Cursor, *, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """What still applies here. Expired rows are filtered, never deleted."""
    cur.execute(
        """
        SELECT * FROM temporary_context
        WHERE expires_at > now()
          AND (scope_id IS NULL OR scope_id = %(scope)s::uuid)
        ORDER BY expires_at
        """,
        {"scope": scope_id},
    )
    return cur.fetchall()
