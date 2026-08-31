"""What a session is handed when it opens (specification 6.1, v3 8).

The whole of the reading path for knowledge, since v2 has no search over it.
That is the trade the seat count pays for: if everything a session needs
arrives before it asks, then the failure v1 could not fix — an agent not
knowing that it does not know, and so never running the query — has nothing
left to happen in.

The rows carry an id and a body and nothing else. v1 measured two thirds of
its opening going to scaffolding around one sentence each, and scaffolding in
a fixed-size opening does not merely cost: it evicts the rules it surrounds.
The id stays because a person reading a stale line needs something to type
after `mashu retire`.

v3 adds the current state of the active tasks, between the memories and the
conditions of the week, and it is the one thing here an agent wrote without a
person confirming it. So it arrives wearing a heading built by tasks.py: the
date is inside the delivered text rather than beside it, because a field the
client may drop is not a presentation discipline (v3 3.1). Dormant and closed
tasks are not in this list at all — silence is neither completion nor currency.

The three shares are counted apart and totalled. Nothing here enforces the
total: each share is refused at its own entrance, so an opening over it means
an entrance has stopped holding, and that is worth an event rather than a
quiet delivery.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, memories, projects, tasks, temporary
from mashu.tokens import pushed_cost


def active_states(cur: psycopg.Cursor, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """The active tasks whose state this session is entitled to, in a fixed order.

    A project sits in at most one scope, so a routed session is handed the
    work of its own place plus the work of every project nobody has placed —
    the same rule a temporary context follows, and for the same reason: a
    project that belongs nowhere would otherwise be visible only to sessions
    that are themselves nowhere. A session with no scope is handed all of it,
    because there is nothing to filter on and the whole of it is already
    bounded by the ceiling every write was refused against.

    Archived projects are not excluded. Archiving is a judgement about a
    shelf; what stops a state being delivered is its own lease running out.
    """
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

    # Counted apart because they are refused apart (v3 8). A single number
    # would say the opening fits while hiding which entrance is the one under
    # pressure, and none of the three can be relieved by the other two.
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
        # An invariant nobody can observe is not an invariant (v3 15). The
        # entrances are what keep the total down, so this row is the record
        # that one of them let something past, not a decision about the
        # opening: it is still delivered whole, since the alternative is
        # dropping rules a session was opened with.
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
    }
