"""Build the session payload from active memories, task state, and temporary context."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, memories, migrate, projects, tasks, temporary, topics
from mashu.tokens import pushed_cost

TASK_DETAIL_INSTRUCTION = (
    "Before continuing any active task, call task_get with its full task_id; "
    "bootstrap carries an index card, not the full current state."
)


def active_states(cur: psycopg.Cursor, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """The active tasks whose state this session is entitled to, in a fixed order."""
    rows = tasks.task_list(cur, activity="active")
    homes = {
        row["project_id"]: row["scope_id"]
        for row in projects.list_projects(cur, include_archived=True)
    }
    # Unscoped projects apply everywhere. Scoped projects stay in their delivery boundary;
    # an unrouted session therefore receives only the unscoped rows.
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
    index = topics.session_index(cur, scope_id)
    states = active_states(cur, scope_id)
    contexts = temporary.active_temporary(cur, scope_id=scope_id)

    cur.execute("SELECT count(*) AS n FROM nomination WHERE status = 'pending'")
    pending = cur.fetchone()["n"]

    # Where the schema stands, delivered rather than looked up.
    schema_pending = migrate.pending(cur)

    # Counted apart because they are refused apart (v3 8). Topic lines sit in the Memory seats.
    topic_tokens = pushed_cost([row["line"] for row in index])
    memory_tokens = pushed_cost([row["content"] for row in always + scoped]) + topic_tokens
    state_rows = [
        {
            "task": str(row["task"]["task_id"])[:8],
            "task_id": row["task"]["task_id"],
            "heading": row["heading"],
            "content": tasks.card_text(row["task"]["name"], row["state"]),
        }
        for row in states
    ]
    project_tokens = sum(tasks.card_cost(row["task"]["name"], row["state"]) for row in states)
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
            "topics": topic_tokens,
            "project": project_tokens,
            "cards": project_tokens,
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
                "cards": project_tokens,
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
        "topics": index,
        "topic_instruction": topics.INSTRUCTION if index else None,
        "scope": scope_name,
        "routed": routed,
        "states": state_rows,
        "task_instruction": TASK_DETAIL_INSTRUCTION if state_rows else None,
        "temporary": [{"content": r["content"], "expires_at": r["expires_at"]} for r in contexts],
        "pending": pending,
        "memory_tokens": memory_tokens,
        "topic_tokens": topic_tokens,
        "card_tokens": project_tokens,
        "project_tokens": project_tokens,
        "temporary_tokens": temporary_tokens,
        "tokens": total,
        "capacity": ceiling,
        "over_budget": over,
        "schema_pending": schema_pending,
    }
