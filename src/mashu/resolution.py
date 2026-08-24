"""Deciding whether an entity already exists (specification 20).

Creating entities automatically is forbidden. The reason is not that duplicates
are untidy: two entities for one concept means two active versions answering as
current, and section 10 says there is exactly one current answer. A duplicate
does not look like a conflict, it looks like agreement, so nothing surfaces it.

What happens here is only the first half of the section. Candidates above the
threshold are found and handed back; the agent chooses. Choosing to create
anyway is allowed and produces a provisional entity, which works normally but
stays out of layer 1 until a review settles it (20.1).

The threshold is deliberately not fixed by the specification. It is a property
of the embedding model, measured in 27.2 against deliberately confusable
titles. The values below are placeholders that let the pipeline run before that
measurement exists, and they are the first thing 27.2 replaces.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg

from mashu.embed import get_embedder
from mashu.errors import MashuError

THRESHOLD_ENV_VAR = "MASHU_RESOLUTION_THRESHOLD"

#: Measured, not guessed (27.2, `mashu admin thresholds`). Against the real
#: store the same-scope pairs of distinct concepts run to a maximum of 0.921,
#: and ten rewordings of real titles run from 0.891 up. The two overlap, so no
#: value separates them and the number is a choice about which error to make.
#:
#: The sweep: 0.89 catches all ten rewordings and flags 11 of 1046 real pairs,
#: 0.90 catches eight and flags five, 0.94 catches one and flags none. The
#: placeholder was 0.94, which is to say it was off.
#:
#: 0.90 is taken because a missed duplicate compounds — the store fragments and
#: the same knowledge sits under two titles — while a false flag costs one
#: decision and, at 0.90, lands on pairs that read as genuinely adjacent. It
#: wants revisiting once something decides what an unattended capture does with
#: a flag, since that is what sets the price of the flag.
PROVISIONAL_THRESHOLDS = {
    "intfloat/multilingual-e5-large": 0.90,
    "hashing": 0.60,
}
FALLBACK_THRESHOLD = 0.90


class SimilarEntityError(MashuError):
    """Entities close enough to the proposed title already exist (20).

    Carries them so the agent can add a version to one instead. Creating anyway
    is permitted, and produces a provisional entity.
    """

    def __init__(self, message: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.candidates = candidates


def threshold() -> float:
    """The similarity above which creating a new entity needs review."""
    configured = os.environ.get(THRESHOLD_ENV_VAR)
    if configured:
        return float(configured)
    return PROVISIONAL_THRESHOLDS.get(get_embedder().name, FALLBACK_THRESHOLD)


_SIMILAR_SQL = """
SELECT memory_id, type, title, status,
       1 - (title_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity
WHERE scope_id = %(scope_id)s
  AND status IN ('active', 'provisional')
  AND title_embedding IS NOT NULL
ORDER BY title_embedding <=> %(q)s::vector
LIMIT %(limit)s
"""


def find_similar(
    cur: psycopg.Cursor,
    *,
    scope_id: UUID,
    title: str,
    limit: int = 5,
    minimum: float | None = None,
) -> list[dict[str, Any]]:
    """Existing entities in the scope whose titles resemble this one.

    Merged and archived entities are left out. An archived entity is one the
    user has already decided should not answer, and proposing a version on it
    would quietly revive that decision.
    """
    vector = get_embedder().embed_documents([title])[0]
    cur.execute(
        _SIMILAR_SQL,
        {
            "q": "[" + ",".join(f"{v:.8f}" for v in vector) + "]",
            "scope_id": scope_id,
            "limit": limit,
        },
    )
    rows = cur.fetchall()
    floor = threshold() if minimum is None else minimum
    return [row for row in rows if row["similarity"] >= floor]


def needs_similar_review(
    cur: psycopg.Cursor, *, scope_id: UUID, title: str
) -> list[dict[str, Any]]:
    """The candidates that would make a new entity provisional, if any."""
    return find_similar(cur, scope_id=scope_id, title=title)


#: Centred cosine above which two titles in one scope are worth putting in
#: front of a reviewer side by side.
#:
#: This is not the creation threshold and cannot share its number. Raw cosine
#: on this embedding is anisotropic — unrelated titles in the real store sit
#: around 0.82 and the nearest raw neighbour of an unrelated proposal reached
#: 0.898 — so a raw floor either shows everything or nothing. Subtracting the
#: store's mean title vector before comparing separates it: over the 42
#: proposals waiting when this was measured, 0.45 gave 6 of them a neighbour,
#: at most two each, and every pair it surfaced read as genuinely adjacent.
#: Identical titles do not go through this at all: equality is not a
#: measurement, and the four such pairs in the store came from one source being
#: ingested twice, which is the case this most needs to catch.
LOOK_ALIKE_FLOOR = 0.45

#: How many to show. This is a note in the margin of a decision about one
#: proposal, not a search result.
LOOK_ALIKE_LIMIT = 3

_LOOK_ALIKE_SQL = """
WITH mu AS (
    SELECT avg(title_embedding) AS m
    FROM memory_entity
    WHERE status IN ('active', 'provisional') AND title_embedding IS NOT NULL
),
pool AS (
    SELECT e.memory_id, e.title, e.type, e.title_embedding,
           e.status AS entity_status,
           v.status AS version_status,
           l.status AS latest_status
    FROM memory_entity e
    LEFT JOIN memory_version v ON v.version_id = e.active_version
    LEFT JOIN memory_version l ON l.version_id = e.latest_version
    WHERE e.scope_id = %(scope_id)s
      AND e.status IN ('active', 'provisional')
      AND e.title_embedding IS NOT NULL
)
SELECT a.memory_id AS of_memory, b.memory_id, b.title, b.type,
       b.entity_status, b.version_status, b.latest_status,
       b.title = a.title AS same_title,
       CASE WHEN b.title = a.title THEN 1.0
            ELSE 1 - ((a.title_embedding - mu.m) <=> (b.title_embedding - mu.m))
       END AS similarity
FROM pool a
JOIN pool b ON b.memory_id <> a.memory_id
CROSS JOIN mu
WHERE a.memory_id = ANY(%(ids)s)
  AND (b.title = a.title
       OR 1 - ((a.title_embedding - mu.m) <=> (b.title_embedding - mu.m)) >= %(floor)s)
ORDER BY a.memory_id, similarity DESC
"""


def look_alikes(
    cur: psycopg.Cursor,
    *,
    scope_id: UUID,
    memory_ids: list[UUID],
    floor: float | None = None,
    limit: int = LOOK_ALIKE_LIMIT,
) -> dict[UUID, list[dict[str, Any]]]:
    """What else in the scope already says something close to this (20).

    Section 20 keeps entity creation out of the automatic path because one
    concept answering under two entities is the failure this store exists to
    prevent. The check runs when a proposal is written, and by review time its
    result is gone: the reader is shown a proposal on its own and has to
    remember two hundred others to notice it is the same claim again.

    The mean title vector is subtracted before comparing. Without that the
    numbers are unusable — see LOOK_ALIKE_FLOOR — and with it the same
    arithmetic runs in the database, so nothing here has to hold the store in
    memory to answer.

    Answers for many memories at once because a review sitting asks about a
    whole bundle, and the mean is over the store rather than the scope so that
    a scope holding three memories still gets a usable one.
    """
    if not memory_ids:
        return {}
    cur.execute(
        _LOOK_ALIKE_SQL,
        {
            "scope_id": scope_id,
            "ids": list(memory_ids),
            "floor": LOOK_ALIKE_FLOOR if floor is None else floor,
        },
    )
    out: dict[UUID, list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        # An identical title is equality and not a measurement, so it is kept
        # whatever the arithmetic says. Everything else has to be a number: a
        # store too small to estimate a mean from can centre a vector onto the
        # origin, and the cosine of that is not a low score but no score.
        if not row["same_title"] and row["similarity"] != row["similarity"]:
            continue
        near = out.setdefault(row["of_memory"], [])
        if len(near) < limit:
            near.append(row)
    return out


def adopted_pairs(cur: psycopg.Cursor, *, floor: float | None = None) -> list[dict[str, Any]]:
    """Look-alikes among what the store already holds as true (20, 30 段 B).

    The check in section 20 runs when a proposal is written and is spent by the
    time anybody reads it: review shows one proposal at a time, and telling it
    apart from two hundred others is not something a reader can do from memory.
    Whatever slips through is then adopted and never measured again. Ten of
    seventy-seven memories in one scope of the real store had a look-alike
    sitting beside them, and nothing in the system was in a position to say so.

    Answers in pairs rather than per memory, because the decision is about the
    two together: either they are one concept and one of them should be folded
    into the other, or they are two and saying so should stop them coming back.
    Ordered by similarity, so the most alike is read first.
    """
    cur.execute(_ADOPTED_SQL)
    by_scope: dict[UUID, list[UUID]] = {}
    held: dict[UUID, dict[str, Any]] = {}
    for row in cur.fetchall():
        by_scope.setdefault(row["scope_id"], []).append(row["memory_id"])
        held[row["memory_id"]] = row

    apart = _told_apart(cur)
    seen: set[frozenset[UUID]] = set()
    pairs: list[dict[str, Any]] = []
    for scope_id, ids in by_scope.items():
        found = look_alikes(cur, scope_id=scope_id, memory_ids=ids, floor=floor)
        for memory_id, near in found.items():
            for other in near:
                key = frozenset((memory_id, other["memory_id"]))
                # The other side may not be adopted — look_alikes reads the
                # whole scope — and a pair whose other half is a candidate is
                # review's business rather than the sweep's.
                if len(key) < 2 or key in seen or key in apart:
                    continue
                if other["memory_id"] not in held:
                    continue
                seen.add(key)
                pairs.append(
                    {
                        "scope_name": held[memory_id]["scope_name"],
                        "similarity": other["similarity"],
                        "same_title": other["same_title"],
                        "left": held[memory_id],
                        "right": held[other["memory_id"]],
                    }
                )
    pairs.sort(key=lambda pair: (not pair["same_title"], -(pair["similarity"] or 0)))
    return pairs


def _told_apart(cur: psycopg.Cursor) -> set[frozenset[UUID]]:
    """Pairs a person has already examined and found to be two things."""
    cur.execute(
        """
        SELECT memory_id, (detail ->> 'other')::uuid AS other
        FROM event_log
        WHERE event_type = 'told_apart' AND detail ? 'other'
        """
    )
    return {frozenset((row["memory_id"], row["other"])) for row in cur.fetchall()}


_ADOPTED_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, s.name AS scope_name,
       e.active_version AS version_id, coalesce(v.directive, v.content) AS content
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version
JOIN scope s ON s.scope_id = e.scope_id
WHERE e.status = 'active' AND s.status = 'active'
ORDER BY s.name, e.title
"""
