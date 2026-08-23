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

#: Placeholders until 27.2 measures them. Similarity scales differ by model,
#: so a single number across models would be wrong for all but one of them.
PROVISIONAL_THRESHOLDS = {
    "intfloat/multilingual-e5-large": 0.94,
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
