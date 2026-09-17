"""Finding the same hole twice."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config


def _floor() -> float:
    """The lowest score worth fetching, computed per call."""
    return min(config.SHOW_THRESHOLD, config.match_threshold())


_LEDGER = """
SELECT l.*, similarity(l.prevention, %(text)s) AS score
FROM ledger l
WHERE similarity(l.prevention, %(text)s) >= %(floor)s
  AND (%(exclude)s::uuid IS NULL OR l.ledger_id <> %(exclude)s::uuid)
ORDER BY score DESC, l.created_at DESC
LIMIT %(limit)s
"""

_TRACES = """
SELECT t.*, similarity(t.content, %(text)s) AS score
FROM trace t
WHERE t.expires_at > now()
  AND similarity(t.content, %(text)s) >= %(floor)s
  AND (%(exclude)s::uuid IS NULL OR t.trace_id <> %(exclude)s::uuid)
ORDER BY score DESC, t.created_at DESC
LIMIT %(limit)s
"""

# Only four columns, and content is not among them.
_TOMBSTONES = """
SELECT m.memory_id, m.retire_reason, m.retired_at, similarity(m.content, %(text)s) AS score
FROM memory m
WHERE m.status = 'retired'
  AND similarity(m.content, %(text)s) >= %(floor)s
ORDER BY score DESC, m.retired_at DESC
LIMIT %(limit)s
"""

_PENDING = """
SELECT n.*, similarity(n.content, %(text)s) AS score
FROM nomination n
WHERE n.status = 'pending'
  AND similarity(n.content, %(text)s) >= %(floor)s
ORDER BY score DESC, n.created_at
LIMIT %(limit)s
"""

# The body is returned here, unlike the tombstones above, and the difference is not an
# inconsistency.
_ACTIVE = """
SELECT m.memory_id, m.content, m.delivery, m.guard_action, m.scope_id,
       similarity(m.content, %(text)s) AS score
FROM memory m
WHERE m.status = 'active'
  AND similarity(m.content, %(text)s) >= %(floor)s
ORDER BY score DESC, m.created_at
LIMIT %(limit)s
"""


def similar_ledger(
    cur: psycopg.Cursor, text: str, *, exclude: UUID | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Past pains whose prevention reads like this one."""
    cur.execute(_LEDGER, {"text": text, "floor": _floor(), "exclude": exclude, "limit": limit})
    return cur.fetchall()


def similar_traces(
    cur: psycopg.Cursor, text: str, *, exclude: UUID | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Live traces that look like this. Expired rows make no claim and are skipped."""
    cur.execute(_TRACES, {"text": text, "floor": _floor(), "exclude": exclude, "limit": limit})
    return cur.fetchall()


def similar_tombstones(cur: psycopg.Cursor, text: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Retired memories this resembles, as reasons rather than as claims."""
    cur.execute(_TOMBSTONES, {"text": text, "floor": _floor(), "limit": limit})
    return cur.fetchall()


def similar_pending_nominations(
    cur: psycopg.Cursor, text: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    """Candidates already waiting on a person for roughly this rule."""
    cur.execute(_PENDING, {"text": text, "floor": _floor(), "limit": limit})
    return cur.fetchall()


def similar_active_memories(
    cur: psycopg.Cursor, text: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    """Rules already being delivered that say roughly this."""
    cur.execute(_ACTIVE, {"text": text, "floor": _floor(), "limit": limit})
    return cur.fetchall()
