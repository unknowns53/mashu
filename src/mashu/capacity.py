"""Enforce capacity limits for memories delivered at session start."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import psycopg

from mashu import config
from mashu.errors import MashuError
from mashu.tokens import ROW_OVERHEAD, pushed_cost

#: The advisory lock namespace this module and the pain pipeline share.
LOCK_NAMESPACE = 271828
#: Serialises the seat check against every other seat check.
LOCK_ADMISSION = 1
#: Serialises the pain pipeline: ledger insert, matching, and the nomination that may follow.
LOCK_PAIN = 2
#: Serialises retirement changes with conflict checks used during admission and restoration.
LOCK_RETIREMENT = 5

_PUSHED = """
SELECT memory_id, delivery, scope_id, topic_id, content FROM memory
WHERE status = 'active' AND delivery IN ('always', 'scope', 'topic')
"""

_TOPICS = """
SELECT topic_id, name, trigger, scope_id FROM topic WHERE archived_at IS NULL
"""


def topic_line(name: str, rules: int, trigger: str) -> str:
    """The line a topic pushes. Bootstrap sends exactly this, so the count matches it."""
    noun = "rule" if rules == 1 else "rules"
    return f"{name} ({rules} {noun}): {trigger}"


@dataclass(frozen=True)
class _Opening:
    """The pushed rows and the topics they may sit in, as stored or as a write would leave them."""

    rows: tuple[dict[str, Any], ...]
    topics: dict[Any, dict[str, Any]]

    def without_memory(self, memory_id: UUID | None) -> _Opening:
        if memory_id is None:
            return self
        return replace(self, rows=tuple(r for r in self.rows if r["memory_id"] != memory_id))

    def with_row(self, row: dict[str, Any]) -> _Opening:
        return replace(self, rows=(*self.rows, row))

    def with_topic(self, topic_id: UUID, topic: dict[str, Any] | None) -> _Opening:
        topics = {key: value for key, value in self.topics.items() if key != topic_id}
        if topic is not None:
            topics[topic_id] = topic
        return replace(self, topics=topics)


def _opening(cur: psycopg.Cursor) -> _Opening:
    cur.execute(_PUSHED)
    rows = tuple(cur.fetchall())
    cur.execute(_TOPICS)
    return _Opening(rows=rows, topics={row["topic_id"]: row for row in cur.fetchall()})


def _measure(opening: _Opening) -> dict[str, Any]:
    """Every session's share, each scope's share, and each topic's unpushed bodies."""
    always: list[str] = []
    scoped: dict[Any, list[str]] = {}
    bodies: dict[Any, list[str]] = {}
    for row in opening.rows:
        if row["delivery"] == "always":
            always.append(row["content"])
        elif row["delivery"] == "scope":
            scoped.setdefault(row["scope_id"], []).append(row["content"])
        elif row["topic_id"] in opening.topics:
            bodies.setdefault(row["topic_id"], []).append(row["content"])
    for topic_id, contents in bodies.items():
        topic = opening.topics[topic_id]
        line = topic_line(topic["name"], len(contents), topic["trigger"])
        if topic["scope_id"] is None:
            always.append(line)
        else:
            scoped.setdefault(topic["scope_id"], []).append(line)
    return {
        "always": pushed_cost(always),
        "scopes": {scope_id: pushed_cost(contents) for scope_id, contents in scoped.items()},
        "bodies": {topic_id: pushed_cost(contents) for topic_id, contents in bodies.items()},
    }


def bootstrap_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What the opening currently costs: everywhere, per scope, and at its worst."""
    measured = _measure(_opening(cur))
    return {
        "always": measured["always"],
        "scopes": measured["scopes"],
        "worst": measured["always"] + max(measured["scopes"].values(), default=0),
    }


def _room(projected: int, cost: int, what: str) -> str:
    """The arithmetic every refusal opens with."""
    return (
        f"this would take it to {projected} "
        f"({cost} for {what} on top of {projected - cost} already pushed). "
    )


def _free(layer: str, free: int) -> str:
    """What is left of a share before this write, which is what the write has to fit in."""
    if free < 0:
        return f"{layer} has no tokens free and is {-free} over already. "
    return f"{layer} has {free} tokens free. "


@dataclass(frozen=True)
class _Doors:
    """The ways out a refusal names, in the commands of the surface that made the write."""

    retire: str
    scope: str
    topic: str
    elsewhere: str
    note: str = ""


_DOORS = {
    "cli": _Doors(
        retire="`mashu retire <id> --reason <why>` for a rule that has stopped earning its seat",
        scope="`mashu deliver <id> scope --scope <name>` for one that only governs one place",
        topic="`mashu deliver <id> topic --topic <name>` for one needed only during one kind "
        "of work",
        elsewhere="`mashu deliver <id> topic --topic <other>`, or always or scope",
    ),
    "mcp": _Doors(
        retire="memory_change_propose with operation='retire' for a rule that has stopped "
        "earning its seat",
        scope="memory_redeliver with delivery='scope' for one that only governs one place",
        topic="memory_redeliver with delivery='topic' for one needed only during one kind of work",
        elsewhere="memory_redeliver with another topic, or always or scope",
        note=" Each changes an existing Memory, so it needs the user's instruction; a retire "
        "proposal is applied with memory_change_apply.",
    ),
}
_SURFACE: ContextVar[str] = ContextVar("mashu_refusal_surface", default="cli")


@contextmanager
def doors(surface: str) -> Iterator[None]:
    """Name the doors out of a refusal in this surface's commands ('cli' or 'mcp')."""
    token = _SURFACE.set(surface)
    try:
        yield
    finally:
        _SURFACE.reset(token)


def _refusal(projected: int, cost: int, ceiling: int, what: str, detail: str = "") -> str:
    """A refusal that names the numbers and the doors out."""
    out = _DOORS[_SURFACE.get()]
    return (
        f"the opening seats {ceiling} tokens and "
        + _room(projected, cost, what)
        + detail
        + f"Make room first: {out.retire}, or {out.topic}."
        + out.note
    )


def _refusal_always(projected: int, cost: int, ceiling: int, what: str, detail: str = "") -> str:
    """The same refusal, plus the door only a rule in this layer has."""
    out = _DOORS[_SURFACE.get()]
    return (
        f"the always layer seats {ceiling} tokens and "
        + _room(projected, cost, what)
        + detail
        + f"Make room first: {out.retire}, {out.scope}, or {out.topic}."
        + out.note
    )


def _refusal_topic(name: str, projected: int, cost: int, ceiling: int) -> str:
    """A topic's bodies are bounded apart from the opening, with their own doors out."""
    out = _DOORS[_SURFACE.get()]
    return (
        f"the topic '{name}' holds {ceiling} tokens of rules and this would take it to "
        f"{projected} ({cost} for this content on top of {projected - cost} already there). "
        f"Make room first: {out.retire}, or move one to another topic or delivery "
        f"({out.elsewhere})." + out.note
    )


def _topic_costs(opening: _Opening) -> str:
    """What one more rule under each topic listed everywhere would add to the always layer."""
    held = Counter(row["topic_id"] for row in opening.rows if row["delivery"] == "topic")
    costs = []
    for topic_id, topic in sorted(opening.topics.items(), key=lambda item: item[1]["name"]):
        if topic["scope_id"] is not None:
            continue
        rules = held.get(topic_id, 0)
        before = pushed_cost([topic_line(topic["name"], rules, topic["trigger"])]) if rules else 0
        after = pushed_cost([topic_line(topic["name"], rules + 1, topic["trigger"])])
        costs.append(f"'{topic['name']}' +{after - before}")
    listed = f": {', '.join(costs)}" if costs else ""
    return (
        "Filed under a topic instead, it would cost this layer only that topic's index line"
        f"{listed}. A new topic adds its whole line '<name> (1 rule): <trigger>', "
        f"{ROW_OVERHEAD} tokens plus one per 4 characters (one per CJK character), and a topic "
        "listed in one scope costs this layer nothing. "
    )


def _line_detail(line: str, first: bool, scoped: bool) -> str:
    """The index line a topic write pushes, said whole so its cost needs no second try."""
    told = (
        "An empty topic is not listed, so its first rule adds its whole index line"
        if first
        else "Its index line becomes"
    )
    shorter = (
        "A shorter trigger shortens that line"
        if scoped
        else "A shorter trigger, or listing the topic in one scope, shortens or moves that line"
    )
    return f"{told} '{line}' ({pushed_cost([line])} tokens). {shorter}. "


def _seat(measured: dict[str, Any], scope_id: Any) -> int:
    return measured["always"] + measured["scopes"].get(scope_id, 0)


def _judge(
    before: _Opening,
    after: _Opening,
    *,
    without: _Opening,
    cost: int,
    what: str,
    scope_id: Any,
    everywhere: bool,
    topic_id: UUID | None = None,
    detail: str = "",
) -> dict[str, Any]:
    """Refuse a write only where it makes a share heavier than its ceiling allows.

    A write that leaves a share no heavier passes even when that share is already over,
    because ceilings and line lengths move without any write (v3 5.3), and refusing the
    write that makes room would close the only way out. An opening refusal says what was
    free before the write, then the caller's `detail` on what the write adds.
    """
    was, now, rest = _measure(before), _measure(after), _measure(without)
    ceiling = config.capacity()

    if topic_id is not None:
        body = now["bodies"].get(topic_id, 0)
        body_ceiling = config.topic_capacity()
        if body > body_ceiling and body > was["bodies"].get(topic_id, 0):
            added = body - rest["bodies"].get(topic_id, 0)
            name = after.topics[topic_id]["name"]
            return {
                "ok": False,
                "tokens": cost,
                "projected": body,
                "capacity": body_ceiling,
                "refusal": _refusal_topic(name, body, added, body_ceiling),
            }

    layer_ceiling = config.always_capacity()
    if now["always"] > layer_ceiling and now["always"] > was["always"]:
        return {
            "ok": False,
            "tokens": cost,
            "projected": now["always"],
            "capacity": layer_ceiling,
            "refusal": _refusal_always(
                now["always"],
                now["always"] - rest["always"],
                layer_ceiling,
                what,
                _free("The always layer", layer_ceiling - rest["always"]) + detail,
            ),
        }

    touched = {None, *now["scopes"], *was["scopes"]}
    heavier = [
        key for key in touched if _seat(now, key) > ceiling and _seat(now, key) > _seat(was, key)
    ]
    if heavier:
        worst = max(heavier, key=lambda key: _seat(now, key))
        projected = _seat(now, worst)
        return {
            "ok": False,
            "tokens": cost,
            "projected": projected,
            "capacity": ceiling,
            "refusal": _refusal(
                projected,
                projected - _seat(rest, worst),
                ceiling,
                what,
                _free("That opening", ceiling - _seat(rest, worst)) + detail,
            ),
        }

    if everywhere:
        projected = now["always"] + max(now["scopes"].values(), default=0)
    else:
        projected = _seat(now, scope_id)
    return {
        "ok": True,
        "tokens": cost,
        "projected": projected,
        "capacity": ceiling,
        "refusal": None,
    }


def _lock(cur: psycopg.Cursor) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_ADMISSION))


def check_admission(
    cur: psycopg.Cursor,
    *,
    content: str,
    delivery: str,
    scope_id: UUID | None = None,
    exclude_memory_id: UUID | None = None,
    topic_id: UUID | None = None,
) -> dict[str, Any]:
    """Whether this content can take a seat, and what it would cost if it did.

    A topic Memory is weighed twice: its body against the topic's own bound, and its
    topic's index line against the opening that line is pushed into.
    """
    _lock(cur)
    cost = pushed_cost([content])
    before = _opening(cur)
    without = before.without_memory(exclude_memory_id)

    if delivery not in ("always", "scope", "topic"):
        raise MashuError(f"unknown delivery '{delivery}'")
    if delivery == "topic":
        topic = before.topics.get(topic_id)
        if topic is None:
            raise MashuError("a topic Memory needs a topic that exists and is not archived")
        scope_id = topic["scope_id"]
        rules = 1 + sum(
            1 for r in without.rows if r["delivery"] == "topic" and r["topic_id"] == topic_id
        )
        line = topic_line(topic["name"], rules, topic["trigger"])
        detail = _line_detail(line, rules == 1, scope_id is not None)
    else:
        detail = _topic_costs(without) if delivery == "always" else ""
    row = {
        "memory_id": exclude_memory_id,
        "delivery": delivery,
        "scope_id": scope_id,
        "topic_id": topic_id if delivery == "topic" else None,
        "content": content,
    }
    return _judge(
        before,
        without.with_row(row),
        without=without,
        cost=cost,
        what="this topic's index line" if delivery == "topic" else "this content",
        scope_id=scope_id,
        everywhere=delivery == "always" or (delivery == "topic" and scope_id is None),
        topic_id=topic_id if delivery == "topic" else None,
        detail=detail,
    )


def check_topic_edit(
    cur: psycopg.Cursor,
    topic_id: UUID,
    *,
    name: str,
    trigger: str,
    scope_id: UUID | None,
) -> dict[str, Any]:
    """Whether a topic's line can change as asked; an empty topic pushes nothing."""
    _lock(cur)
    before = _opening(cur)
    current = before.topics.get(topic_id)
    if current is None:
        raise MashuError("only a topic that is not archived can be edited")
    edited = {**current, "name": name, "trigger": trigger, "scope_id": scope_id}
    after = before.with_topic(topic_id, edited)
    rules = sum(1 for row in before.rows if row["topic_id"] == topic_id)
    line = topic_line(name, rules, trigger)
    return _judge(
        before,
        after,
        without=before.with_topic(topic_id, None),
        cost=pushed_cost([line]) if rules else 0,
        what="this topic's index line",
        scope_id=scope_id,
        everywhere=scope_id is None,
        detail=f"The edited index line reads '{line}' ({pushed_cost([line])} tokens). ",
    )
