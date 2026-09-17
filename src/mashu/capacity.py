"""Enforce capacity limits for memories delivered at session start."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config
from mashu.errors import MashuError
from mashu.tokens import pushed_cost

#: The advisory lock namespace this module and the pain pipeline share.
LOCK_NAMESPACE = 271828
#: Serialises the seat check against every other seat check.
LOCK_ADMISSION = 1
#: Serialises the pain pipeline: ledger insert, matching, and the nomination that may follow.
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
    """A refusal that names the numbers and both doors out."""
    return (
        f"the opening seats {ceiling} tokens and "
        + _room(projected, cost)
        + f"Make room first: {_RETIRE}, or {_GUARD}."
    )


def _refusal_always(projected: int, cost: int, ceiling: int) -> str:
    """The same refusal, plus the door only a rule in this layer has."""
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
    """Whether this content can take a seat, and what it would cost if it did."""
    # Lock before calculating capacity.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_ADMISSION))

    cost = pushed_cost([content])
    ceiling = config.capacity()
    totals = _totals(cur, exclude_memory_id)

    if delivery == "guard":
        # Guard memories are not part of the session payload.
        return {
            "ok": True,
            "tokens": cost,
            "projected": totals["worst"],
            "capacity": ceiling,
            "refusal": None,
        }
    if delivery == "always":
        # Check the always-layer limit before the overall limit.
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
