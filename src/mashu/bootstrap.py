"""What a session is handed when it opens (specification 6.1).

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
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, memories, temporary
from mashu.tokens import pushed_cost


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
    contexts = temporary.active_temporary(cur, scope_id=scope_id)

    cur.execute("SELECT count(*) AS n FROM nomination WHERE status = 'pending'")
    pending = cur.fetchone()["n"]

    tokens = pushed_cost([row["content"] for row in always + scoped])
    ceiling = config.capacity()

    events.record(
        cur,
        "bootstrap_served",
        actor,
        detail={"tokens": tokens, "scope": scope_name},
    )
    return {
        "always": [{"memory_id": r["memory_id"], "content": r["content"]} for r in always],
        "scoped": [{"memory_id": r["memory_id"], "content": r["content"]} for r in scoped],
        "scope": scope_name,
        "routed": routed,
        "temporary": [{"content": r["content"], "expires_at": r["expires_at"]} for r in contexts],
        "pending": pending,
        "tokens": tokens,
        "capacity": ceiling,
        "over_budget": tokens > ceiling,
    }
