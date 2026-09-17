"""Build the session payload from active memories, task state, and temporary context."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, memories, migrate, projects, tasks, temporary
from mashu.tokens import pushed_cost


def active_states(cur: psycopg.Cursor, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """The active tasks whose state this session is entitled to, in a fixed order."""
    rows = tasks.task_list(cur, activity="active")
    if scope_id is None:
        return rows
    homes = {
        row["project_id"]: row["scope_id"]
        for row in projects.list_projects(cur, include_archived=True)
    }
    return [row for row in rows if homes.get(row["task"]["project_id"]) in (None, scope_id)]


def session_bootstrap(
    cur: psycopg.Cursor,
    *,
    actor: str,
    scope_id: UUID | None = None,
    scope_name: str | None = None,
    routed: bool = False,
) -> dict[str, Any]:
    """Everything a session in this scope is told, and what it cost to tell it."""
    always = memories.always_memories(cur)
    scoped = memories.scope_push_memories(cur, scope_id) if scope_id is not None else []
    states = active_states(cur, scope_id)
    contexts = temporary.active_temporary(cur, scope_id=scope_id)

    cur.execute("SELECT count(*) AS n FROM nomination WHERE status = 'pending'")
    pending = cur.fetchone()["n"]

    # Where the schema stands, delivered rather than looked up.
    schema_pending = migrate.pending(cur)

    # Counted apart because they are refused apart (v3 8).
    memory_tokens = pushed_cost([row["content"] for row in always + scoped])
    state_rows = [
        {
            "task": str(row["task"]["task_id"])[:8],
            "heading": row["heading"],
            "content": tasks.state_text(row["task"]["name"], row["state"]),
        }
        for row in states
    ]
    project_tokens = sum(tasks.state_cost(row["task"]["name"], row["state"]) for row in states)
    temporary_tokens = pushed_cost([row["content"] for row in contexts])
    total = memory_tokens + project_tokens + temporary_tokens
    ceiling = config.total_capacity()
    over = total > ceiling

    events.record(
        cur,
        "bootstrap_served",
        actor,
        detail={
            "tokens": total,
            "memory": memory_tokens,
            "project": project_tokens,
            "temporary": temporary_tokens,
            "scope": scope_name,
        },
    )
    if over:
        # An invariant nobody can observe is not an invariant (v3 15).
        events.record(
            cur,
            "bootstrap_over_capacity",
            actor,
            detail={
                "tokens": total,
                "capacity": ceiling,
                "memory": memory_tokens,
                "project": project_tokens,
                "temporary": temporary_tokens,
            },
        )
    if schema_pending:
        # Count sessions opened against a newer schema to track migration lag.
        events.record(
            cur,
            "bootstrap_schema_behind",
            actor,
            detail={"pending": schema_pending},
        )
    return {
        "always": [{"memory_id": r["memory_id"], "content": r["content"]} for r in always],
        "scoped": [{"memory_id": r["memory_id"], "content": r["content"]} for r in scoped],
        "scope": scope_name,
        "routed": routed,
        "states": state_rows,
        "temporary": [{"content": r["content"], "expires_at": r["expires_at"]} for r in contexts],
        "pending": pending,
        "memory_tokens": memory_tokens,
        "project_tokens": project_tokens,
        "temporary_tokens": temporary_tokens,
        "tokens": total,
        "capacity": ceiling,
        "over_budget": over,
        "schema_pending": schema_pending,
    }
