"""Session Bootstrap: the context handed over before any query (specification 21.2).

Retrieval is query-driven, so it answers nothing until an agent asks. An agent
that does not know what the store holds has no reason to ask, which is why
section 6.1 calls the pull path dead without this. Bootstrap is the push half:
a fixed payload delivered at session start whether the agent asks or not.

Three parts, and the third is the one that matters most. Preferences and the
current state are knowledge; the scope index is a *map* of the knowledge, and
it is the map that gives every later memory_search a motive.

Nothing here embeds anything. Bootstrap has no query to encode, so it reads
rows and counts characters and never touches the model. Session start is the
one moment where a cold model load would be paid by every session, and it is
also the one moment with nothing to encode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from mashu.events import record
from mashu.models import EventType, MemoryType
from mashu.retrieval import estimate_tokens

#: Section 21.2. Bootstrap is a fixed cost paid by every session, so without a
#: ceiling the budget grows with the store. Re-measured by the switchover trial
#: (27.5), which is the first time the real per-session cost is observable.
BOOTSTRAP_TOKEN_BUDGET = 2000

# The index carries each scope's lifecycle because an empty answer from a
# seeding scope otherwise reads as "nothing is known about this", which is the
# reading that sends an agent off to derive what a review is about to adopt.
_SCOPE_INDEX_SQL = """
SELECT scope_id, name, description, lifecycle
FROM scope
WHERE status = 'active'
ORDER BY name
"""

# Both payload queries read the pointer, never the version status: active is
# what active_version points at and nowhere else (specification 10).
_ACTIVE_BY_TYPE_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.content
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version AND v.memory_id = e.memory_id
WHERE e.status = 'active'
  AND e.type = %(type)s
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
ORDER BY e.title
"""


@dataclass
class Bootstrapped:
    """What a session is handed before it asks anything."""

    scope_index: list[dict[str, Any]] = field(default_factory=list)
    preferences: list[dict[str, Any]] = field(default_factory=list)
    current_state: list[dict[str, Any]] = field(default_factory=list)
    #: Memory IDs whose content was dropped to stay inside the budget. They are
    #: still listed, by title, so memory_get can fetch what was cut.
    trimmed: list[UUID] = field(default_factory=list)
    #: What the payload costs as handed over, after any trimming. The
    #: pre-trim figure is not the session's cost and would not be the
    #: number 27.5 needs to re-measure the ceiling against.
    tokens: int = 0
    #: True when trimming ran out of content to drop and the payload is still
    #: over the ceiling. The scope index is never trimmed (21.2), so an index
    #: that alone exceeds the budget has no way down; saying so is the least a
    #: fixed cost can do when it stops being fixed.
    over_budget: bool = False

    def memory_ids(self) -> list[UUID]:
        """Everything named in the payload, trimmed or whole."""
        return [row["memory_id"] for row in (*self.preferences, *self.current_state)]


def session_bootstrap(
    cur: psycopg.Cursor,
    *,
    actor: str,
    scopes: list[UUID] | None = None,
    budget: int = BOOTSTRAP_TOKEN_BUDGET,
    record_event: bool = True,
) -> Bootstrapped:
    """Assemble the fixed session-start context and log what was handed over.

    scopes narrows the current state to the scopes a session already knows it
    is working in. Leaving it out is the ordinary case: a session that has not
    started cannot know its scope yet, and the index is what tells it.
    """
    cur.execute(_SCOPE_INDEX_SQL)
    scope_index = [
        {
            "scope_id": row["scope_id"],
            "name": row["name"],
            "summary": _one_line(row["description"]),
            "lifecycle": str(row["lifecycle"]),
        }
        for row in cur.fetchall()
    ]

    params = {"scopes": scopes or None}
    cur.execute(_ACTIVE_BY_TYPE_SQL, dict(params, type=str(MemoryType.PREFERENCE)))
    preferences = cur.fetchall()
    cur.execute(_ACTIVE_BY_TYPE_SQL, dict(params, type=str(MemoryType.STATE)))
    current_state = cur.fetchall()

    trimmed = _fit(scope_index, preferences, current_state, budget=budget)
    result = Bootstrapped(
        scope_index=scope_index,
        preferences=preferences,
        current_state=current_state,
        trimmed=trimmed,
        tokens=_total_tokens(scope_index, preferences, current_state),
    )
    result.over_budget = result.tokens > budget

    if record_event:
        record(
            cur,
            EventType.SESSION_BOOTSTRAPPED,
            actor,
            detail={
                "scopes": [str(s) for s in (scopes or [])],
                "scope_index": [str(row["scope_id"]) for row in scope_index],
                "memories": [str(m) for m in result.memory_ids()],
                "trimmed": [str(m) for m in trimmed],
                "tokens": result.tokens,
                "over_budget": result.over_budget,
            },
        )
    return result


def _one_line(description: str | None) -> str:
    """A scope's summary as one line. An empty description is not an error."""
    if not description:
        return ""
    return " ".join(description.split())


def _fit(
    scope_index: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
    current_state: list[dict[str, Any]],
    *,
    budget: int,
) -> list[UUID]:
    """Drop content until the payload fits, in place. Returns what was cut.

    Section 21.2 keeps the whole scope index and spends the shortfall on the
    other two. The map is what makes the rest reachable: an agent missing a
    preference body can still fetch it by ID, while an agent missing the index
    does not know there is anything to fetch.

    Two orderings are chosen here that the section leaves open.

    Current state gives up its content before preferences do. A preference the
    agent cannot read is misbehaviour on every turn afterwards and nobody
    notices; a current state it cannot read costs one lookup at the start of
    work in that scope, and the work itself makes the gap obvious.

    Within a group the largest goes first, because cutting the biggest item
    buys the most budget per item lost, and the count of items still readable
    whole is what the trimming is trying to protect.
    """
    trimmed: list[UUID] = []
    total = _total_tokens(scope_index, preferences, current_state)
    if total <= budget:
        return trimmed

    for group in (current_state, preferences):
        for row in sorted(group, key=lambda r: -estimate_tokens(r["content"] or "")):
            if total <= budget:
                return trimmed
            total -= estimate_tokens(row["content"] or "")
            row["content"] = None
            trimmed.append(row["memory_id"])
    return trimmed


def _total_tokens(
    scope_index: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
    current_state: list[dict[str, Any]],
) -> int:
    """What the payload costs as it currently stands."""
    cost = sum(estimate_tokens(f"{row['name']} {row['summary']}") for row in scope_index)
    for row in (*preferences, *current_state):
        cost += estimate_tokens(row["title"]) + estimate_tokens(row["content"] or "")
    return cost
