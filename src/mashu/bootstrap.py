"""Build the session payload from active memories, task state, and temporary context."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import (
    config,
    events,
    memories,
    migrate,
    projects,
    task_history,
    tasks,
    temporary,
    topics,
)
from mashu.tokens import pushed_cost

TASK_DETAIL_INSTRUCTION = (
    "Before continuing any active task, call task_get with its full task_id; "
    "bootstrap carries an index card, not the full current state."
)

UNSEATED_INSTRUCTION = (
    "These active tasks are older than the cards above and are named only; call task_get "
    "with one's id before continuing it, and task_search finds the rest."
)


def _unseated_lines(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Name as many cardless tasks as the overflow share holds, and count the rest."""
    named: list[dict[str, Any]] = []
    budget = config.card_overflow_capacity() - pushed_cost([UNSEATED_INSTRUCTION])
    for row in rows:
        line = {
            "task": str(row["task_id"])[:8],
            "task_id": row["task_id"],
            "name": row["name"],
            "last_activity": row["last_activity_at"].date(),
        }
        if pushed_cost([_unseated_text(r) for r in [*named, line]]) > budget:
            break
        named.append(line)
    return named, len(rows) - len(named)


def _unseated_text(line: dict[str, Any]) -> str:
    return f"{line['task']} {line['name']} ({line['last_activity']:%Y-%m-%d})"


def active_states(cur: psycopg.Cursor, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """The active tasks whose state this session is entitled to, in a fixed order."""
    cur.execute(
        "SELECT to_regclass('task') IS NOT NULL AS tasks, "
        "to_regclass('task_close_proposal') IS NOT NULL AS proposals"
    )
    available = cur.fetchone()
    if not available["tasks"]:
        return []
    rows = tasks.task_list(cur, activity="active", include_proposals=available["proposals"])
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
    decided = task_history.decision_count(cur, [row["task"]["task_id"] for row in states])
    for row in states:
        row["state"] = {
            **row["state"],
            "decisions_in_force_count": decided.get(row["task"]["task_id"], 0),
        }
    costed = [
        {
            "task_id": row["task"]["task_id"],
            "name": row["task"]["name"],
            "last_activity_at": row["task"]["last_activity_at"],
            "tokens": tasks.card_cost(row["task"]["name"], row["state"]),
            "row": row,
        }
        for row in states
    ]
    seated, unseated = tasks.seat_cards(costed, config.project_capacity())
    state_rows = [
        {
            "task": str(card["task_id"])[:8],
            "task_id": card["task_id"],
            "heading": card["row"]["heading"],
            "content": tasks.card_text(card["name"], card["row"]["state"]),
        }
        for card in seated
    ]
    named, unnamed = _unseated_lines(unseated)
    project_tokens = sum(card["tokens"] for card in seated) + (
        pushed_cost([UNSEATED_INSTRUCTION, *(_unseated_text(line) for line in named)])
        if unseated
        else 0
    )
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
            "unseated": len(unseated),
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
        "unseated": named,
        "unseated_more": unnamed,
        "unseated_instruction": UNSEATED_INSTRUCTION if unseated else None,
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
