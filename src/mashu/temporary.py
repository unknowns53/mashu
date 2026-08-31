"""Conditions that stop applying on their own (specification 7).

The one part of v1 kept unchanged, because it never had the problem the rest
had. A temporary context knows its own expiry at the moment it is written, so
it needs no review to enter and no upkeep to leave: this week's maintenance
window, tomorrow's reset quota. Nobody has to remember to withdraw it.

The fourteen-day ceiling is the whole of the discipline on how long one lasts.
A claim that wants longer is not temporary; it is a permanent claim wearing an
expiry date so it can skip the evidence it would otherwise need, and the
refusal says where it belongs instead.

How many may stand at once is a second ceiling, and it is this share's own
(v3 8). In v2 they sat inside the memory seat count, which meant a week of
outages could refuse a rule that had cost something to learn. The room here is
small and separate: neither side borrows from the other, and both are refused
at their own door rather than trimmed at delivery.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, redact
from mashu.errors import RefusedError
from mashu.tokens import pushed_cost

#: Advisory lock class, in the namespace capacity.py opened. Classes 1 to 3
#: there and in tasks.py are the seat check, the pain pipeline and the project
#: state; this fourth one serialises the temporary share's own check, so two
#: writers cannot both read room for the last of it.
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
    """The conditions still applying, in the two buckets they are weighed in.

    Unscoped ones ride with every session, which is what 'always' means for a
    memory and means here too.
    """
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
    """What the standing conditions cost: everywhere, per scope, and at worst.

    The same shape and the same reasoning as the memory totals. An unscoped
    condition reaches every session, so it weighs on the heaviest scope's
    opening as well as its own, and 'worst' is the number that has to stay
    under the ceiling for the ceiling to mean anything.
    """
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
    """Refuse a condition the share has no room for.

    Nothing retires here and nothing is trimmed, so the refusal names the only
    two exits there are: the next expiry, which needs nobody's decision, and
    saying this one shorter. The expiry named is the first among the
    conditions this one is actually weighed against — waiting on one that
    weighs somewhere else would free no room here.
    """
    # Before reading, and held until the write commits, for the reason the
    # seat check takes its lock: the gap between finding room and taking it is
    # where two writers fit into the same room.
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

    # Nothing to wait for when this condition overruns the share on its own,
    # and telling somebody to wait would be telling them to wait forever.
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

    # Pushed, so it is weighed before it is written (v3 8). Expiring on its
    # own is why it needs no review; it is not a reason to be weightless in an
    # opening it is being carried in. What it is weighed against is the
    # temporary share alone, and the memories are weighed against theirs.
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
