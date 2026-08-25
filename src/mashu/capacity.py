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
stepping down to guard is one of the two ways out a refusal names.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config
from mashu.errors import MashuError
from mashu.tokens import pushed_cost

_PUSHED = """
SELECT delivery, scope_id, content FROM memory
WHERE status = 'active' AND delivery IN ('always', 'scope')
  AND (%(exclude)s::uuid IS NULL OR memory_id <> %(exclude)s::uuid)
"""


def _totals(cur: psycopg.Cursor, exclude: UUID | None) -> dict[str, Any]:
    cur.execute(_PUSHED, {"exclude": exclude})
    always: list[str] = []
    scoped: dict[Any, list[str]] = {}
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


def _refusal(projected: int, cost: int, ceiling: int) -> str:
    """A refusal that names the numbers and both doors out.

    A gate that only says no teaches nothing, and the person reading it is
    holding a rule they believe is worth keeping. The two ways through are
    real commands, not advice.
    """
    return (
        f"the opening seats {ceiling} tokens and this would take it to {projected} "
        f"({cost} for this content on top of {projected - cost} already pushed). "
        "Make room first: `mashu retire <id> --reason <why>` for a rule that has "
        "stopped earning its seat, or `mashu deliver <id> guard --action <act>` to "
        "move one out of the opening and in front of the act it governs."
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
        projected = totals["always"] + cost + max(totals["scopes"].values(), default=0)
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
