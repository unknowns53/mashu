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

from mashu import config


def _floor() -> float:
    """The lowest score worth fetching, computed per call.

    Normally SHOW_THRESHOLD, which sits below the deciding threshold: showing a
    reporter a weak match costs a glance. But match_threshold() is calibrated
    against real entries and may be tuned below it, and a fixed retrieval floor
    would then hide rows the decision logic was about to act on — dedupe and
    rederivation would quietly stop firing with nothing to see. Whichever is
    lower wins, so retrieval can never be the narrower of the two.
    """
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

# The body is returned here, unlike the tombstones above, and the difference
# is not an inconsistency. A retired memory's content is a claim that was
# withdrawn; an active one is being pushed into every session that matches it
# right now. Handing it back to somebody reporting that they were hurt by not
# knowing it tells them what they were supposed to have been holding — which
# is the whole of what makes the collision readable as a delivery failure
# rather than as one more pain.
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
    """Rules already being delivered that say roughly this.

    A pain landing here is the third occurrence the specification says
    falsifies the entrance standard rather than confirming it (12): the rule
    was admitted, it is being pushed, and it did not arrive. Section 4.1 is
    about proving a second pain; this is about noticing that a proven one did
    not stay fixed.
    """
    cur.execute(_ACTIVE, {"text": text, "floor": _floor(), "limit": limit})
    return cur.fetchall()
