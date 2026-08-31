"""The seat count (specification 5.2).

A ceiling, but not a budget. In v1 what did not fit fell through to search;
here there is no search, so the ceiling is the thing that keeps the whole
store small enough to be pushed in full. Refusing an admission is therefore
not a deferral — it is the mechanism.

What an addition is weighed against depends on where it lands. A scope rule
rides with one scope's sessions, so it competes with that scope. An always
rule rides with every session in turn, including the session that opens in the
heaviest scope, so the fullest scope is what it has to fit alongside. A guard
rule rides with neither: it is not in the opening at all, which is why
stepping down to guard is one of the ways out a refusal names.

What it counts is the memories, and only those. v2 weighed the temporary
contexts here as well, on the ground that they are pushed in the same opening;
v3 8 keeps the arithmetic and moves it, because one subsystem quietly spending
another's room is the failure the split ceilings exist to prevent — a fortnight
of expiring conditions could refuse a rule admitted against evidence. Each
share is now refused at its own entrance and none of them borrows.

The always layer answers to a second, lower ceiling of its own. Left with only
the shared one it never overflows; it quietly spends the whole store's headroom
on rules that most sessions did not need, and the scopes find the room gone
without anything having refused them. Counting rows was the earlier shorthand
for this and it drifted: the rows came out shorter than the estimate they stood
in for, so the count held the layer near half the size it was meant to have.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config
from mashu.errors import MashuError
from mashu.tokens import pushed_cost

#: The advisory lock namespace this module and the pain pipeline share. Two
#: classes, taken for the whole transaction so they hold until the write they
#: guard commits.
LOCK_NAMESPACE = 271828
#: Serialises the seat check against every other seat check. Without it two
#: sessions read the same totals, both find room for the last seat, and both
#: sit down: the ceiling is checked twice and enforced never.
LOCK_ADMISSION = 1
#: Serialises the pain pipeline: ledger insert, matching, and the nomination
#: that may follow. Held by ledger.report_pain and by the agent-carried
#: instruction path, which are the two writers that read the queue to decide
#: whether to add to it.
LOCK_PAIN = 2

_PUSHED = """
SELECT delivery, scope_id, content FROM memory
WHERE status = 'active' AND delivery IN ('always', 'scope')
  AND (%(exclude)s::uuid IS NULL OR memory_id <> %(exclude)s::uuid)
"""


def _totals(cur: psycopg.Cursor, exclude: UUID | None) -> dict[str, Any]:
    always: list[str] = []
    scoped: dict[Any, list[str]] = {}

    cur.execute(_PUSHED, {"exclude": exclude})
    for row in cur.fetchall():
        if row["delivery"] == "always":
            always.append(row["content"])
        else:
            scoped.setdefault(row["scope_id"], []).append(row["content"])

    scopes = {scope_id: pushed_cost(contents) for scope_id, contents in scoped.items()}
    always_cost = pushed_cost(always)
    return {
        "always": always_cost,
        "scopes": scopes,
        "worst": always_cost + max(scopes.values(), default=0),
    }


def bootstrap_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What the opening currently costs: everywhere, per scope, and at its worst."""
    return _totals(cur, None)


def _room(projected: int, cost: int) -> str:
    """The arithmetic both refusals open with."""
    return (
        f"this would take it to {projected} "
        f"({cost} for this content on top of {projected - cost} already pushed). "
    )


#: The door out that every refusal names: a rule that has stopped earning its seat.
_RETIRE = "`mashu retire <id> --reason <why>` for a rule that has stopped earning its seat"
#: The door only guard offers, and the reason it is never itself full.
_GUARD = (
    "`mashu deliver <id> guard --action <act>` to move one out of the opening "
    "and in front of the act it governs"
)


def _refusal(projected: int, cost: int, ceiling: int) -> str:
    """A refusal that names the numbers and both doors out.

    A gate that only says no teaches nothing, and the person reading it is
    holding a rule they believe is worth keeping. The two ways through are
    real commands, not advice.
    """
    return (
        f"the opening seats {ceiling} tokens and "
        + _room(projected, cost)
        + f"Make room first: {_RETIRE}, or {_GUARD}."
    )


def _refusal_always(projected: int, cost: int, ceiling: int) -> str:
    """The same refusal, plus the door only a rule in this layer has.

    A rule refused here is not too big for the store; it is too big for the
    part of the store every session pays for. So the way out a scope rule
    does not have is the useful one: if it governs one place, say which.
    """
    return (
        f"the always layer seats {ceiling} tokens and "
        + _room(projected, cost)
        + f"Make room first: {_RETIRE}, "
        "`mashu deliver <id> scope --scope <name>` for one that only governs "
        f"one place, or {_GUARD}."
    )


def check_admission(
    cur: psycopg.Cursor,
    *,
    content: str,
    delivery: str,
    scope_id: UUID | None = None,
    exclude_memory_id: UUID | None = None,
) -> dict[str, Any]:
    """Whether this content can take a seat, and what it would cost if it did.

    `exclude_memory_id` is what makes revision-in-place possible: a memory
    being rewritten is not competing with itself, and without the exclusion
    every edit to a full store would be refused for the space it already
    occupies.
    """
    # Before reading anything. The gap between deciding there is room and
    # taking it is where two writers both fit into one seat, and every write
    # that changes the opening comes through here inside the caller's single
    # transaction, so holding until commit closes the gap rather than narrowing
    # it.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_ADMISSION))

    cost = pushed_cost([content])
    ceiling = config.capacity()
    totals = _totals(cur, exclude_memory_id)

    if delivery == "guard":
        # Not in the opening, so nothing to weigh it against.
        return {
            "ok": True,
            "tokens": cost,
            "projected": totals["worst"],
            "capacity": ceiling,
            "refusal": None,
        }
    if delivery == "always":
        # Two ceilings, checked nearest first so the refusal names the one that
        # actually bites. A rule can clear the layer and still not clear the
        # opening it shares with the heaviest scope; the reverse is what the
        # layer ceiling exists to catch.
        layer = totals["always"] + cost
        layer_ceiling = config.always_capacity()
        if layer > layer_ceiling:
            return {
                "ok": False,
                "tokens": cost,
                "projected": layer,
                "capacity": layer_ceiling,
                "refusal": _refusal_always(layer, cost, layer_ceiling),
            }
        projected = layer + max(totals["scopes"].values(), default=0)
    elif delivery == "scope":
        projected = totals["always"] + totals["scopes"].get(scope_id, 0) + cost
    else:
        raise MashuError(f"unknown delivery '{delivery}'")

    ok = projected <= ceiling
    return {
        "ok": ok,
        "tokens": cost,
        "projected": projected,
        "capacity": ceiling,
        "refusal": None if ok else _refusal(projected, cost, ceiling),
    }
