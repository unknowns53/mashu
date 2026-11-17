"""Manage expiring context conditions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, redact, settings
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
SELECT context_id, project_id, content, expires_at
FROM temporary_context WHERE expires_at > now()
"""


def _standing(cur: psycopg.Cursor) -> tuple[list[dict[str, Any]], dict[Any, list[dict[str, Any]]]]:
    """The conditions still applying, in the two buckets they are weighed in."""
    always: list[dict[str, Any]] = []
    by_project: dict[Any, list[dict[str, Any]]] = {}
    cur.execute(_UNEXPIRED)
    for row in cur.fetchall():
        bucket = (
            always if row["project_id"] is None else by_project.setdefault(row["project_id"], [])
        )
        bucket.append(row)
    return always, by_project


def _cost(rows: list[dict[str, Any]]) -> int:
    return pushed_cost([row["content"] for row in rows])


def pushed_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What the standing conditions cost: everywhere, per project, and at worst."""
    always, by_project = _standing(cur)
    projects = {project_id: _cost(rows) for project_id, rows in by_project.items()}
    always_cost = _cost(always)
    return {
        "always": always_cost,
        "projects": projects,
        "worst": always_cost + max(projects.values(), default=0),
        "count": len(always) + sum(len(rows) for rows in by_project.values()),
    }


def _check_room(
    cur: psycopg.Cursor,
    *,
    content: str,
    project_id: UUID | None,
    exclude_context_id: UUID | None = None,
) -> None:
    """Refuse a condition the share has no room for."""
    # Lock before checking capacity and hold it through the write.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))

    always, by_project = _standing(cur)
    if exclude_context_id is not None:
        always = [row for row in always if row["context_id"] != exclude_context_id]
        by_project = {
            key: [row for row in rows if row["context_id"] != exclude_context_id]
            for key, rows in by_project.items()
        }
    if project_id is None:
        heaviest = max(by_project.values(), key=_cost, default=[])
        against = always + heaviest
    else:
        against = always + by_project.get(project_id, [])

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


def put_temporary(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    days: float,
    project_id: UUID | None = None,
) -> dict[str, Any]:
    """Record a condition together with the moment it stops being one."""
    if not 0 < days <= config.TEMPORARY_MAX_DAYS:
        raise RefusedError(_TOO_LONG.format(max=config.TEMPORARY_MAX_DAYS, asked=f"{days:g} days"))
    if not content.strip():
        raise MashuError("temporary context cannot be empty")
    verdict = redact.gate({"content": content})

    # Pushed, so it is weighed before it is written (project-state 8).
    _check_room(cur, content=content, project_id=project_id)

    cur.execute(
        """
        INSERT INTO temporary_context (content, project_id, created_by, expires_at)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s))
        RETURNING *
        """,
        (content, project_id, actor, float(days) * 86400.0),
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
    project_id: UUID | None,
    actor: str,
) -> dict[str, Any]:
    """Replace a standing condition's editable text and project, keeping its expiry."""
    content = (content or "").strip()
    if not content:
        raise MashuError("temporary context cannot be empty")
    verdict = redact.gate({"content": content})
    # convert_to_memory takes this lock before the row; taking them in the same order here
    # keeps the two from waiting on each other.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))
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
        project_id=project_id,
        exclude_context_id=context_id,
    )
    cur.execute(
        """
        UPDATE temporary_context SET content = %s, project_id = %s
        WHERE context_id = %s RETURNING *
        """,
        (content, project_id, context_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "temporary_revised",
        actor,
        detail={
            "context_id": str(context_id),
            "from_project_id": str(current["project_id"]) if current["project_id"] else None,
            "to_project_id": str(project_id) if project_id else None,
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
    delivery = "project" if current["project_id"] else "always"
    memory = memories.remember(
        cur,
        content=current["content"],
        actor=actor,
        project_id=current["project_id"],
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


def active_temporary(
    cur: psycopg.Cursor, *, project_id: UUID | None = None
) -> list[dict[str, Any]]:
    """What still applies here. Expired rows are filtered, never deleted."""
    cur.execute(
        """
        SELECT * FROM temporary_context
        WHERE expires_at > now()
          AND (project_id IS NULL OR project_id = %(project)s::uuid)
        ORDER BY expires_at
        """,
        {"project": project_id},
    )
    return cur.fetchall()
