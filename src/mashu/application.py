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
    tasks,
    temporary,
)


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
    traces: int
    ledger_30d: int
    delivery_failures_30d: int
    scope_count: int
    scope_rows: list[dict[str, Any]]
    tasks_active: int
    tasks_dormant: int
    tasks_closed: int


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
    cur.execute("SELECT count(*) AS n FROM trace WHERE expires_at > now()")
    traces = cur.fetchone()["n"]
    cur.execute("SELECT count(*) AS n FROM ledger WHERE created_at >= now() - interval '30 days'")
    ledger_30d = cur.fetchone()["n"]
    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = 'delivery_failure_suspected' "
        "AND created_at >= now() - interval '30 days'"
    )
    delivery_failures = cur.fetchone()["n"]
    scope_rows = scopes.list_scopes(cur)
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
        traces=traces,
        ledger_30d=ledger_30d,
        delivery_failures_30d=delivery_failures,
        scope_count=len(scope_rows),
        scope_rows=scope_rows,
        tasks_active=task_counts["active"],
        tasks_dormant=task_counts["dormant"],
        tasks_closed=task_counts["closed"],
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
