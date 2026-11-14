"""Application queries and actions shared by commands and terminal screens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from mashu import (
    bootstrap,
    capacity,
    migrate,
    routing,
    scopes,
    settings,
    tasks,
    temporary,
    topics,
)

#: How suspected delivery failures are split in status.
DELIVERY_FAILURE_ROUTES = ("pushed", "topic_unread", "topic_read", "guard", "unknown")

# How the rule behind a delivery_failure_suspected event reached the session. A topic rule that
# failed unread points at its trigger; one that failed after being read points at its wording,
# so the two are counted apart (memory 10). An unread topic linked to an action is the one the
# PreToolUse hook stands behind, so it counts as guard.
FAILURE_ROUTE_SQL = """CASE
    WHEN detail->>'delivery' IN ('always', 'scope') THEN 'pushed'
    WHEN detail->>'delivery' = 'guard' THEN 'guard'
    WHEN detail->>'topic_read' = 'true' THEN 'topic_read'
    WHEN detail->>'topic_action' IS NOT NULL THEN 'guard'
    WHEN detail->>'topic_read' = 'false' THEN 'topic_unread'
    ELSE 'unknown'
END"""


@dataclass(frozen=True)
class StatusSnapshot:
    schema_pending: tuple[str, ...]
    memory_counts: dict[str, int]
    memory_always_tokens: int
    memory_worst_tokens: int
    active_states: int
    state_worst_tokens: int
    temporary_count: int
    temporary_tokens: int
    pending_ready: int
    pending_deferred: int
    ledger_30d: int
    delivery_failures_30d: int
    delivery_failures_by_route: dict[str, int]
    knowledge_unused_30d: int
    scope_count: int
    scope_rows: list[dict[str, Any]]
    topic_count: int
    topic_heaviest_name: str | None
    topic_heaviest_tokens: int
    tasks_active: int
    tasks_dormant: int
    tasks_closed: int
    limits: dict[str, int]


def memory_view(cur: psycopg.Cursor, view: str) -> list[dict[str, Any]]:
    """Read one complete view, including the scope names people recognize."""
    if view == "temporary":
        cur.execute(
            """
            SELECT t.*, s.name AS scope_name
            FROM temporary_context t LEFT JOIN scope s ON s.scope_id = t.scope_id
            WHERE t.expires_at > now()
            ORDER BY t.expires_at, t.context_id
            """
        )
    else:
        cur.execute(
            """
            SELECT m.*, s.name AS scope_name,
                   t.name AS topic_name, t.trigger AS topic_trigger
            FROM memory m
            LEFT JOIN scope s ON s.scope_id = m.scope_id
            LEFT JOIN topic t ON t.topic_id = m.topic_id
            WHERE m.status = %s
            ORDER BY m.delivery, s.name NULLS FIRST, t.name, m.created_at, m.memory_id
            """,
            (view,),
        )
    return cur.fetchall()


#: The orders each list can be put in, the default first. Shared by the terminal screens and
#: the desktop app so both offer the same keys.
MEMORY_SORTS = {
    "active": ("updated", "created", "delivery"),
    "retired": ("updated", "created", "kind"),
    "temporary": ("created", "expires", "scope"),
}
TASK_SORTS = ("updated", "created", "project")
PROJECT_SORTS = ("created", "name")
#: Keys that hold a time; they start newest first, and the rest start A to Z.
DATE_SORTS = frozenset({"updated", "created", "expires"})
_DELIVERY_ORDER = ("always", "scope", "topic")


def sort_value(row: dict[str, Any], key: str) -> Any:
    """What one Memory, temporary context, task view, or project row is ordered by."""
    task = row.get("task")
    if task is not None:
        if key == "created":
            return task["created_at"]
        if key == "project":
            return task["project_name"]
        return task.get("last_activity_at") or row["state"]["updated_at"]
    if key == "updated":
        return row.get("updated_at") or row.get("created_at")
    if key == "expires":
        return row.get("expires_at")
    if key == "delivery":
        place = row.get("topic_name") or row.get("scope_name") or ""
        return (_DELIVERY_ORDER.index(row["delivery"]), place)
    if key == "kind":
        return row.get("retirement_kind") or "legacy"
    if key == "scope":
        return row.get("scope_name") or ""
    return row.get(key if key == "name" else "created_at")


def sort_rows(rows: list[dict[str, Any]], key: str, *, descending: bool) -> list[dict[str, Any]]:
    """Rows in order of `key`, rows without a value last either way.

    A key many rows share, such as delivery or project, keeps each group newest first, whichever
    way the groups run, since the latest change is what a person looks for inside one.
    """
    if key not in DATE_SORTS:
        rows = sort_rows(rows, "updated", descending=True)
    keyed = [(sort_value(row, key), row) for row in rows]
    present = [pair for pair in keyed if pair[0] not in (None, "")]
    blank = [row for value, row in keyed if value in (None, "")]
    present.sort(key=lambda pair: pair[0], reverse=descending)
    return [row for _, row in present] + blank


def routed_scope(cur: psycopg.Cursor, cwd: str | None) -> tuple[UUID | None, str | None, bool]:
    """Resolve a directory route to the scope identity and name shown to a person."""
    scope_id, routed = routing.resolve(cur, cwd)
    if scope_id is None:
        return None, None, routed
    cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
    row = cur.fetchone()
    return scope_id, row["name"] if row else None, routed


def bootstrap_preview(cur: psycopg.Cursor, *, cwd: str, actor: str) -> dict[str, Any]:
    """Build the session payload for a directory without duplicating route resolution."""
    scope_id, scope_name, routed = routed_scope(cur, cwd)
    return bootstrap.session_bootstrap(
        cur,
        actor=actor,
        scope_id=scope_id,
        scope_name=scope_name,
        routed=routed,
    )


def missed_knowledge(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """The pains that knowledge already held should have prevented, newest first.

    One report can name a Memory and a task or file together, so each pain carries every
    place it named. `repeats` counts the pains that named the same place, this one included.
    `recent` uses the window the status counts use, so the two agree.
    """
    cur.execute(
        f"""
        WITH missed AS (
            SELECT e.ledger_id, e.event_type, e.memory_id,
                   (e.detail->>'task_id')::uuid AS task_id, e.detail->>'source' AS source,
                   CASE WHEN e.event_type = 'delivery_failure_suspected'
                        THEN {FAILURE_ROUTE_SQL} END AS route
            FROM event_log e
            WHERE e.event_type IN ('delivery_failure_suspected', 'knowledge_unused')
        )
        SELECT x.*, m.content AS memory_content, m.status AS memory_status,
               t.name AS task_name,
               CASE WHEN t.task_id IS NULL THEN NULL WHEN t.status = 'closed' THEN 'closed'
                    WHEN now() > t.active_until THEN 'dormant' ELSE 'active' END AS task_view,
               count(*) OVER (PARTITION BY x.memory_id) AS memory_repeats,
               count(*) OVER (PARTITION BY x.task_id) AS task_repeats,
               count(*) OVER (PARTITION BY x.source) AS source_repeats
        FROM missed x
        LEFT JOIN memory m ON m.memory_id = x.memory_id
        LEFT JOIN task t ON t.task_id = x.task_id
        """
    )
    places: dict[Any, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        named = places.setdefault(row["ledger_id"], [])
        if row["memory_id"] is not None:
            named.append(
                {
                    "kind": "memory",
                    "memory_id": row["memory_id"],
                    "content": row["memory_content"],
                    "status": row["memory_status"],
                    "route": row["route"],
                    "repeats": row["memory_repeats"],
                }
            )
        if row["task_id"] is not None:
            named.append(
                {
                    "kind": "task",
                    "task_id": row["task_id"],
                    "name": row["task_name"],
                    "view": row["task_view"],
                    "repeats": row["task_repeats"],
                }
            )
        if row["source"] is not None:
            named.append(
                {"kind": "source", "source": row["source"], "repeats": row["source_repeats"]}
            )
    if not places:
        return []
    cur.execute(
        """
        SELECT l.ledger_id, l.created_at, l.created_by, l.what, l.prevention,
               EXISTS (
                   SELECT 1 FROM event_log e
                   WHERE e.ledger_id = l.ledger_id
                     AND e.event_type IN ('delivery_failure_suspected', 'knowledge_unused')
                     AND e.created_at >= now() - interval '30 days'
               ) AS recent,
               l.prevention_kind, l.filed_task, t.name AS filed_task_name,
               CASE WHEN t.task_id IS NULL THEN NULL WHEN t.status = 'closed' THEN 'closed'
                    WHEN now() > t.active_until THEN 'dormant' ELSE 'active' END AS filed_task_view
        FROM ledger l LEFT JOIN task t ON t.task_id = l.filed_task
        WHERE l.ledger_id = ANY(%s)
        ORDER BY l.created_at DESC, l.ledger_id
        """,
        (list(places),),
    )
    return [{**row, "places": places[row["ledger_id"]]} for row in cur.fetchall()]


def status_snapshot(cur: psycopg.Cursor) -> StatusSnapshot:
    """Collect the store health numbers used by both status views."""
    schema_pending = tuple(migrate.pending(cur))
    cur.execute(
        "SELECT delivery, count(*) AS n FROM memory "
        "WHERE status = 'active' GROUP BY delivery ORDER BY delivery"
    )
    memory_counts = {row["delivery"]: row["n"] for row in cur.fetchall()}
    memory_totals = capacity.bootstrap_totals(cur)
    state_totals = tasks.pushed_totals(cur)
    temporary_totals = temporary.pushed_totals(cur)
    cur.execute(
        "SELECT count(*) FILTER (WHERE deferred_at IS NULL) AS ready, "
        "count(*) FILTER (WHERE deferred_at IS NOT NULL) AS deferred "
        "FROM nomination WHERE status = 'pending'"
    )
    pending = cur.fetchone()
    cur.execute("SELECT count(*) AS n FROM ledger WHERE created_at >= now() - interval '30 days'")
    ledger_30d = cur.fetchone()["n"]
    cur.execute(
        f"""
        SELECT {FAILURE_ROUTE_SQL} AS route, count(*) AS n
        FROM event_log
        WHERE event_type = 'delivery_failure_suspected'
          AND created_at >= now() - interval '30 days'
        GROUP BY 1
        """
    )
    by_route = {row["route"]: row["n"] for row in cur.fetchall()}
    delivery_failures = sum(by_route.values())
    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = 'knowledge_unused' "
        "AND created_at >= now() - interval '30 days'"
    )
    knowledge_unused = cur.fetchone()["n"]
    scope_rows = scopes.list_scopes(cur)
    topic_rows = topics.list_topics(cur)
    heaviest = max(topic_rows, key=lambda row: row["body_tokens"], default=None)
    cur.execute(
        "SELECT count(*) FILTER (WHERE status = 'open' AND now() <= active_until) AS active, "
        "count(*) FILTER (WHERE status = 'open' AND now() > active_until) AS dormant, "
        "count(*) FILTER (WHERE status = 'closed') AS closed FROM task"
    )
    task_counts = cur.fetchone()
    return StatusSnapshot(
        schema_pending=schema_pending,
        memory_counts=memory_counts,
        memory_always_tokens=memory_totals["always"],
        memory_worst_tokens=memory_totals["worst"],
        active_states=state_totals["count"],
        state_worst_tokens=state_totals["worst"],
        temporary_count=temporary_totals["count"],
        temporary_tokens=temporary_totals["worst"],
        pending_ready=pending["ready"],
        pending_deferred=pending["deferred"],
        ledger_30d=ledger_30d,
        delivery_failures_30d=delivery_failures,
        delivery_failures_by_route=by_route,
        knowledge_unused_30d=knowledge_unused,
        scope_count=len(scope_rows),
        scope_rows=scope_rows,
        topic_count=len(topic_rows),
        topic_heaviest_name=heaviest["name"] if heaviest else None,
        topic_heaviest_tokens=heaviest["body_tokens"] if heaviest else 0,
        tasks_active=task_counts["active"],
        tasks_dormant=task_counts["dormant"],
        tasks_closed=task_counts["closed"],
        limits=settings.limits(cur),
    )


def dashboard_counts(cur: psycopg.Cursor) -> dict[str, int]:
    """The counts the home screens summarise the store with."""
    cur.execute(
        """
        SELECT count(*) FILTER (WHERE deferred_at IS NULL) AS ready,
               count(*) FILTER (WHERE deferred_at IS NOT NULL) AS deferred
        FROM nomination WHERE status = 'pending'
        """
    )
    review = cur.fetchone()

    cur.execute("SELECT count(*) AS n FROM memory_change WHERE status = 'pending'")
    memory_change_count = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT count(*) FILTER (
                   WHERE t.status = 'open' AND now() <= t.active_until
               ) AS active,
               count(*) FILTER (
                   WHERE t.status = 'open' AND now() > t.active_until
               ) AS dormant,
               count(*) FILTER (WHERE t.status = 'closed') AS closed,
               count(cp.task_id) FILTER (WHERE t.status = 'open') AS proposals
        FROM task t
        LEFT JOIN task_close_proposal cp ON cp.task_id = t.task_id
        """
    )
    task = cur.fetchone()

    cur.execute(
        "SELECT count(*) FILTER (WHERE status = 'active') AS active, "
        "count(*) FILTER (WHERE status = 'retired') AS retired FROM memory"
    )
    memory = cur.fetchone()
    cur.execute("SELECT count(*) AS n FROM temporary_context WHERE expires_at > now()")
    temporary_contexts = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM project WHERE archived_at IS NULL")
    project_count = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM scope")
    scope_count = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM route")
    route_count = cur.fetchone()["n"]

    return {
        "review_ready": review["ready"],
        "review_deferred": review["deferred"],
        "memory_changes": memory_change_count,
        "tasks_active": task["active"],
        "tasks_dormant": task["dormant"],
        "task_proposals": task["proposals"],
        "active_memories": memory["active"],
        "projects": project_count,
        "tasks_closed": task["closed"],
        "retired_memories": memory["retired"],
        "temporary_contexts": temporary_contexts,
        "scopes": scope_count,
        "routes": route_count,
        "schema_pending": len(migrate.pending(cur)),
    }


def save_topic(
    cur: psycopg.Cursor,
    topic_id: UUID | None,
    *,
    name: str,
    trigger: str,
    scope_name: str | None,
    actor: str,
    action: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Create a topic, or set every field of one; no scope means every session.

    Editing sets action and source too, so None removes them. A new topic takes neither.
    """
    scope_id = scopes.require_scope(cur, scope_name)["scope_id"] if scope_name else None
    if topic_id is None:
        return topics.create_topic(cur, name=name, trigger=trigger, scope_id=scope_id, actor=actor)
    return topics.update_topic(
        cur,
        topic_id,
        actor=actor,
        name=name,
        trigger=trigger,
        scope_id=scope_id,
        clear_scope=scope_id is None,
        action=action,
        clear_action=action is None,
        source=source,
        clear_source=source is None,
    )


def set_route(
    cur: psycopg.Cursor, *, path_prefix: str, scope_name: str, actor: str
) -> dict[str, Any]:
    """Map a directory tree to a named scope."""
    scope_id = scopes.require_scope(cur, scope_name)["scope_id"]
    return routing.add_route(cur, path_prefix=path_prefix, scope_id=scope_id, actor=actor)


def ignore_route(cur: psycopg.Cursor, *, path_prefix: str, actor: str) -> dict[str, Any]:
    """Mark a directory tree as intentionally outside every scope."""
    return routing.add_route(cur, path_prefix=path_prefix, scope_id=None, actor=actor)


def update_route(
    cur: psycopg.Cursor,
    route_id: UUID,
    *,
    path_prefix: str,
    scope_name: str | None,
    actor: str,
) -> dict[str, Any]:
    """Edit both parts of a route as one application operation."""
    scope_id = scopes.require_scope(cur, scope_name)["scope_id"] if scope_name else None
    return routing.update_route(
        cur,
        route_id,
        path_prefix=path_prefix,
        scope_id=scope_id,
        actor=actor,
    )


def remove_route(cur: psycopg.Cursor, *, path_prefix: str, actor: str) -> bool:
    """Remove a directory mapping, independent of its visible entry point."""
    return routing.remove_route(cur, path_prefix=path_prefix, actor=actor)
