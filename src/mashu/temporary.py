"""Manage expiring context conditions."""

from __future__ import annotations

from collections.abc import Sequence
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
SELECT context_id, project_id, content, expires_at, {also} AS also
FROM temporary_context WHERE expires_at > now()
"""

_ALSO = (
    "ARRAY(SELECT tp.project_id FROM temporary_context_project tp "
    "WHERE tp.context_id = temporary_context.context_id)"
)
_NO_ALSO = "'{}'::uuid[]"

#: The projects a temporary context reaches, its home project first, for a query naming
#: temporary_context t and its home project s.
PROJECT_NAMES = """
    array_remove(ARRAY[s.name] || ARRAY(
        SELECT x.name FROM temporary_context_project tp
        JOIN project x ON x.project_id = tp.project_id
        WHERE tp.context_id = t.context_id ORDER BY x.name
    ), NULL) AS project_names,
    array_to_string(array_remove(ARRAY[s.name] || ARRAY(
        SELECT x.name FROM temporary_context_project tp
        JOIN project x ON x.project_id = tp.project_id
        WHERE tp.context_id = t.context_id ORDER BY x.name
    ), NULL), ', ') AS project_name
"""

_LEGACY_PROJECT_NAMES = """
    array_remove(ARRAY[s.name], NULL) AS project_names,
    s.name AS project_name
"""


def has_other_projects(cur: psycopg.Cursor) -> bool:
    """Whether this store has the temporary_context_project table yet."""
    cur.execute("SELECT to_regclass('temporary_context_project') IS NOT NULL AS present")
    return cur.fetchone()["present"]


def project_names_sql(cur: psycopg.Cursor) -> str:
    """The project_names / project_name projection for a query with aliases t and s."""
    return PROJECT_NAMES if has_other_projects(cur) else _LEGACY_PROJECT_NAMES


def other_projects(cur: psycopg.Cursor, context_id: UUID) -> list[UUID]:
    """The projects a temporary context reaches besides its home project."""
    if not has_other_projects(cur):
        return []
    cur.execute(
        "SELECT project_id FROM temporary_context_project "
        "WHERE context_id = %s ORDER BY project_id",
        (context_id,),
    )
    return [row["project_id"] for row in cur.fetchall()]


def _check_other_projects(project_id: UUID | None, others: Sequence[UUID]) -> list[UUID]:
    others = list(dict.fromkeys(others))
    if others and project_id is None:
        raise MashuError("a temporary context for every session cannot have extra projects")
    return [pid for pid in others if pid != project_id]


def _write_other_projects(
    cur: psycopg.Cursor, context_id: UUID, others: Sequence[UUID], *, actor: str
) -> None:
    if not has_other_projects(cur):
        if others:
            raise MashuError(
                "delivering a temporary context to several projects needs the latest schema; "
                "run 'mashu admin migrate'"
            )
        return
    cur.execute(
        "DELETE FROM temporary_context_project "
        "WHERE context_id = %s AND NOT (project_id = ANY(%s))",
        (context_id, list(others)),
    )
    for pid in others:
        cur.execute(
            "INSERT INTO temporary_context_project (context_id, project_id, created_by) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (context_id, pid, actor),
        )


def _check_days(days: float) -> None:
    if not 0 < days <= config.TEMPORARY_MAX_DAYS:
        raise RefusedError(_TOO_LONG.format(max=config.TEMPORARY_MAX_DAYS, asked=f"{days:g} days"))


def _standing(
    cur: psycopg.Cursor,
) -> tuple[list[dict[str, Any]], dict[Any, list[dict[str, Any]]], int]:
    """The conditions still applying, in the two buckets they are weighed in, plus unique count."""
    always: list[dict[str, Any]] = []
    by_project: dict[Any, list[dict[str, Any]]] = {}
    cur.execute(_UNEXPIRED.format(also=_ALSO if has_other_projects(cur) else _NO_ALSO))
    rows = cur.fetchall()
    for row in rows:
        if row["project_id"] is None:
            always.append(row)
        else:
            for pid in (row["project_id"], *(row.get("also") or ())):
                by_project.setdefault(pid, []).append(row)
    return always, by_project, len(rows)


def _cost(rows: list[dict[str, Any]]) -> int:
    return pushed_cost([row["content"] for row in rows])


def pushed_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What the standing conditions cost: everywhere, per project, and at worst."""
    always, by_project, count = _standing(cur)
    projects = {project_id: _cost(rows) for project_id, rows in by_project.items()}
    always_cost = _cost(always)
    return {
        "always": always_cost,
        "projects": projects,
        "worst": always_cost + max(projects.values(), default=0),
        "count": count,
    }


def _check_room(
    cur: psycopg.Cursor,
    *,
    content: str,
    project_id: UUID | None,
    also_project_ids: Sequence[UUID] = (),
    exclude_context_id: UUID | None = None,
) -> None:
    """Refuse a condition the share has no room for in any session it reaches."""
    # Lock before checking capacity and hold it through the write.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, LOCK_TEMPORARY))

    always, by_project, _ = _standing(cur)
    if exclude_context_id is not None:
        always = [row for row in always if row["context_id"] != exclude_context_id]
        by_project = {
            key: [row for row in rows if row["context_id"] != exclude_context_id]
            for key, rows in by_project.items()
        }
    if project_id is None:
        heaviest = max(by_project.values(), key=_cost, default=[])
        groups = [always + heaviest]
    else:
        groups = [always + by_project.get(pid, []) for pid in (project_id, *also_project_ids)]

    cost = pushed_cost([content])
    ceiling = settings.limit(cur, "temporary_capacity")
    worst = max(groups, key=_cost)
    projected = _cost(worst) + cost
    if projected <= ceiling:
        return

    # A single oversized condition cannot become valid by waiting for expiry.
    waiting = ""
    if worst:
        soonest = min(row["expires_at"] for row in worst)
        waiting = f"the first of them lapses on {soonest.date().isoformat()}, or "
    raise RefusedError(
        f"the temporary share seats {ceiling} tokens and this would take it to {projected} "
        f"({cost} for this condition on top of {projected - cost} already pushed by "
        f"{len(worst)} standing one(s)). Nothing retires here: {waiting}"
        "say this one shorter."
    )


def put_temporary(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    days: float,
    project_id: UUID | None = None,
    also_project_ids: Sequence[UUID] = (),
) -> dict[str, Any]:
    """Record a condition together with the moment it stops being one."""
    _check_days(days)
    if not content.strip():
        raise MashuError("temporary context cannot be empty")
    others = _check_other_projects(project_id, also_project_ids)
    verdict = redact.gate({"content": content})

    # Pushed, so it is weighed before it is written (project-state 8).
    _check_room(cur, content=content, project_id=project_id, also_project_ids=others)

    cur.execute(
        """
        INSERT INTO temporary_context (content, project_id, created_by, expires_at)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s))
        RETURNING *
        """,
        (content, project_id, actor, float(days) * 86400.0),
    )
    row = cur.fetchone()
    if others:
        _write_other_projects(cur, row["context_id"], others, actor=actor)
    detail: dict[str, Any] = {"days": days}
    if others:
        detail["also_project_ids"] = [str(pid) for pid in others]
    events.record(cur, "temporary_recorded", actor, detail=detail)
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
    also_project_ids: Sequence[UUID] | None = None,
    days: float | None = None,
) -> dict[str, Any]:
    """Replace a standing condition's text, projects, and optionally its remaining expiry."""
    content = (content or "").strip()
    if not content:
        raise MashuError("temporary context cannot be empty")
    if days is not None:
        _check_days(days)
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

    before = other_projects(cur, context_id)
    if also_project_ids is None:
        others = before if project_id is not None and project_id == current["project_id"] else []
    else:
        others = _check_other_projects(project_id, also_project_ids)

    _check_room(
        cur,
        content=content,
        project_id=project_id,
        also_project_ids=others,
        exclude_context_id=context_id,
    )
    if days is not None:
        cur.execute(
            """
            UPDATE temporary_context
            SET content = %s, project_id = %s, expires_at = now() + make_interval(secs => %s)
            WHERE context_id = %s RETURNING *
            """,
            (content, project_id, float(days) * 86400.0, context_id),
        )
    else:
        cur.execute(
            """
            UPDATE temporary_context SET content = %s, project_id = %s
            WHERE context_id = %s RETURNING *
            """,
            (content, project_id, context_id),
        )
    row = cur.fetchone()
    if before or others:
        _write_other_projects(cur, context_id, others, actor=actor)
    detail: dict[str, Any] = {
        "context_id": str(context_id),
        "from_project_id": str(current["project_id"]) if current["project_id"] else None,
        "to_project_id": str(project_id) if project_id else None,
    }
    if before or others:
        detail["from_also_project_ids"] = sorted(str(pid) for pid in before)
        detail["to_also_project_ids"] = sorted(str(pid) for pid in others)
    if days is not None:
        detail["days"] = days
    events.record(cur, "temporary_revised", actor, detail=detail)
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
    delivery: str | None = None,
    project_id: UUID | None = None,
    also_project_ids: Sequence[UUID] | None = None,
    topic_id: UUID | None = None,
    why: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Replace a standing condition with a durable memory in one transaction.

    Without an explicit `delivery`, keeps the condition's own reach (always when it applied to
    every session, project with all of its projects otherwise). `acknowledged_conflicts` passes
    through to memories.remember, which refuses a body that repeats a withdrawn or legacy Memory
    until the caller has read those retirements.
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
    body = current["content"] if content is None else content.strip()
    if not body:
        raise MashuError("content is empty; a Memory body states the rule itself")
    if delivery is None:
        delivery = "project" if current["project_id"] else "always"
        target_project = current["project_id"]
        target_others: Sequence[UUID] = (
            other_projects(cur, context_id) if current["project_id"] else ()
        )
    else:
        target_project = project_id
        target_others = also_project_ids or ()
    memory = memories.remember(
        cur,
        content=body,
        actor=actor,
        project_id=target_project,
        delivery=delivery,
        topic_id=topic_id,
        acknowledged_conflicts=acknowledged_conflicts,
        why=why,
        also_project_ids=target_others,
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
    if project_id is not None and has_other_projects(cur):
        cur.execute(
            """
            SELECT * FROM temporary_context
            WHERE expires_at > now()
              AND (project_id IS NULL
                   OR project_id = %(project)s::uuid
                   OR context_id IN (
                       SELECT context_id FROM temporary_context_project
                       WHERE project_id = %(project)s::uuid
                   ))
            ORDER BY expires_at
            """,
            {"project": project_id},
        )
        return cur.fetchall()
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
