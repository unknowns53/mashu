"""Finding the same hole twice.

Trigram similarity, not embeddings. The question this layer asks is narrow —
whether two short pieces of prose describe the same pain — and pg_trgm answers
it without a model, a dimension, or a calibration exercise. v1 carried all
three for a retrieval layer that v2 does not have.

Two floors, and they mean different things. SHOW_THRESHOLD is the point at
which a row is worth putting in front of whoever reported the pain, who can
judge for themselves. match_threshold() is the point at which the code acts on
its own — dedupes a nomination, or calls a second derivation proven — and it
sits higher because nobody is looking.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu.config import SHOW_THRESHOLD

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

# Only four columns, and content is not among them. A retired memory answers
# with why it was retired; handing back the body it was retired for would put
# the withdrawn claim back into circulation with a tombstone attached, and a
# reader who takes the body has not been warned by the reason sitting next to
# it. The withholding is the feature, and this SELECT is where it is enforced.
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


def similar_ledger(
    cur: psycopg.Cursor, text: str, *, exclude: UUID | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Past pains whose prevention reads like this one."""
    cur.execute(
        _LEDGER, {"text": text, "floor": SHOW_THRESHOLD, "exclude": exclude, "limit": limit}
    )
    return cur.fetchall()


def similar_traces(
    cur: psycopg.Cursor, text: str, *, exclude: UUID | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Live traces that look like this. Expired rows make no claim and are skipped."""
    cur.execute(
        _TRACES, {"text": text, "floor": SHOW_THRESHOLD, "exclude": exclude, "limit": limit}
    )
    return cur.fetchall()


def similar_tombstones(cur: psycopg.Cursor, text: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Retired memories this resembles, as reasons rather than as claims."""
    cur.execute(_TOMBSTONES, {"text": text, "floor": SHOW_THRESHOLD, "limit": limit})
    return cur.fetchall()


def similar_pending_nominations(
    cur: psycopg.Cursor, text: str, *, limit: int = 3
) -> list[dict[str, Any]]:
    """Candidates already waiting on a person for roughly this rule."""
    cur.execute(_PENDING, {"text": text, "floor": SHOW_THRESHOLD, "limit": limit})
    return cur.fetchall()
