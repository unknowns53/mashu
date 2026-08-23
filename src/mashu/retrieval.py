"""Reading the knowledge state back out (specifications 21, 21.1).

The pipeline is scope detection, type filter, active version filter, vector
search, ranking, assembly. What comes out is not one list but three, because
hiding a memory and preventing its rediscovery are different things: an agent
that cannot see a refuted hypothesis will derive it again from scratch, and an
agent that cannot see its own pending proposal will file it again.

So the three layers differ in what they hand over, not merely in order.

- layer 1 is current knowledge: content, usable as it stands
- layer 2 is knowledge that has not been reviewed yet: content, tagged
- layer 3 is retired knowledge: the title, the status and the reason, never
  the content. Handing back the content of something disproven invites the
  agent to treat it as current, which is what the active version filter exists
  to prevent

Layer 2 is capped twice (21.1). The absolute cap is a token budget. The
relative cap keeps layer 2 no larger than layer 1, and it is there for the
tag rather than the budget: a tag only changes behaviour while untagged
material sits beside it, so a context that is mostly unreviewed makes the tag
meaningless whichever way the agent reads it.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from mashu import events
from mashu.embed import get_embedder
from mashu.models import EventType, MemoryType

#: How many active memories one query returns before layer 2 is sized.
DEFAULT_LIMIT = 8

#: Absolute cap on layer 2, in tokens (21.1). Provisional; 27.5 revisits it.
LAYER2_TOKEN_BUDGET = 1500

#: Cosine similarity a scope has to reach to be counted as detected.
#: Similarity scales differ by model, so this is per model and provisional in
#: the same way the entity resolution threshold is: 27.2 measures both.
SCOPE_MATCH_THRESHOLDS = {
    "intfloat/multilingual-e5-large": 0.85,
    "hashing": 0.50,
}
SCOPE_MATCH_FALLBACK = 0.80
SCOPE_THRESHOLD_ENV_VAR = "MASHU_SCOPE_THRESHOLD"

UNREVIEWED_TAG = "unreviewed"

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]")


def estimate_tokens(text: str) -> int:
    """Roughly how many tokens a string costs.

    An estimate, deliberately: the real count depends on the tokenizer of
    whichever agent receives the context, and the layer 2 budget only has to
    stop the layer from crowding out layer 1. CJK characters run close to one
    token each; other scripts run nearer four characters to a token.
    """
    cjk = len(_CJK.findall(text))
    return math.ceil(cjk + (len(text) - cjk) / 4)


@dataclass
class Retrieved:
    """One query's answer, kept in three layers (21.1)."""

    query: str
    #: Scopes the query was recognised as being about. Empty means detection
    #: found nothing and layer 1 searched the whole ledger.
    scopes: list[UUID]
    #: Scopes layers 2 and 3 were confined to. When detection found nothing
    #: this is where layer 1 actually landed, which is not the same claim.
    narrowed_to: list[UUID] = field(default_factory=list)
    active: list[dict[str, Any]] = field(default_factory=list)
    unreviewed: list[dict[str, Any]] = field(default_factory=list)
    retired: list[dict[str, Any]] = field(default_factory=list)
    dropped_unreviewed: int = 0

    def memory_ids(self) -> list[UUID]:
        """Every memory handed over, for the event log (22)."""
        return [row["memory_id"] for row in (*self.active, *self.unreviewed, *self.retired)]


# --------------------------------------------------------------------------
# scope detection
# --------------------------------------------------------------------------
def detect_scopes(cur: psycopg.Cursor, query: str) -> list[UUID]:
    """Which scopes in the ledger the query is about (21).

    The ledger is small and read at query time rather than kept embedded: a
    scope's name and description change rarely enough that caching them would
    buy little and go stale silently.

    Returning an empty list means no scope stood out, and the caller searches
    everything. Guessing one scope and being wrong hides the answer completely,
    which is worse than a wider search.
    """
    cur.execute(
        "SELECT scope_id, name, description FROM scope WHERE status = 'active' ORDER BY name"
    )
    scopes = cur.fetchall()
    if not scopes:
        return []

    floor = float(
        os.environ.get(SCOPE_THRESHOLD_ENV_VAR)
        or SCOPE_MATCH_THRESHOLDS.get(get_embedder().name, SCOPE_MATCH_FALLBACK)
    )
    embedder = get_embedder()
    described = [
        f"{s['name']}. {s['description']}" if s["description"] else s["name"] for s in scopes
    ]
    vectors = embedder.embed_documents(described)
    q = embedder.embed_query(query)

    scored = [
        (sum(a * b for a, b in zip(q, v, strict=True)), s["scope_id"])
        for s, v in zip(scopes, vectors, strict=True)
    ]
    return [scope_id for score, scope_id in scored if score >= floor]


# --------------------------------------------------------------------------
# the layers
# --------------------------------------------------------------------------
_LAYER1_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.content,
       1 - (v.content_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version AND v.memory_id = e.memory_id
WHERE e.status = 'active'
  AND v.content_embedding IS NOT NULL
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
  AND (%(types)s::text[] IS NULL OR e.type = ANY(%(types)s::text[]))
ORDER BY v.content_embedding <=> %(q)s::vector
LIMIT %(limit)s
"""

_LAYER2_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.content,
       v.created_by, v.created_at,
       EXTRACT(DAY FROM now() - v.created_at)::int AS days_pending,
       e.status AS entity_status,
       1 - (v.content_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity e
JOIN memory_version v ON v.memory_id = e.memory_id
WHERE e.status IN ('active', 'provisional')
  AND v.status = 'candidate'
  AND (e.active_version IS NULL OR e.active_version <> v.version_id)
  AND v.content_embedding IS NOT NULL
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
  AND (%(types)s::text[] IS NULL OR e.type = ANY(%(types)s::text[]))
ORDER BY v.content_embedding <=> %(q)s::vector
LIMIT %(limit)s
"""

# Layer 3 is ranked on the title, not the content. Ordering by similarity to
# text the layer will not hand over would rank on evidence the agent never
# sees, so the handle it does see is the handle it is ranked by.
_LAYER3_SQL = """
SELECT DISTINCT ON (e.memory_id)
       e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.status, v.reason,
       1 - (e.title_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity e
JOIN memory_version v ON v.memory_id = e.memory_id
WHERE v.status IN ('disproven', 'dormant')
  AND e.title_embedding IS NOT NULL
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
  AND (%(types)s::text[] IS NULL OR e.type = ANY(%(types)s::text[]))
ORDER BY e.memory_id, v.created_at DESC
"""


def retrieve(
    cur: psycopg.Cursor,
    query: str,
    *,
    actor: str,
    scope_id: UUID | None = None,
    types: list[MemoryType] | None = None,
    limit: int = DEFAULT_LIMIT,
    record: bool = True,
) -> Retrieved:
    """Answer one query in three layers and log what was handed over."""
    embedder = get_embedder()
    q = _as_vector(embedder.embed_query(query))
    type_filter = [str(MemoryType(t)) for t in types] if types else None

    scopes = [scope_id] if scope_id is not None else detect_scopes(cur, query)
    params = {
        "q": q,
        "scopes": scopes or None,
        "types": type_filter,
        "limit": limit,
    }

    cur.execute(_LAYER1_SQL, params)
    active = cur.fetchall()

    # Layers 2 and 3 stay inside the scopes layer 1 actually hit. Returning a
    # whole scope's retired memories would let layer 3 crowd the context out
    # (21.1, MVP default).
    hit_scopes = scopes or sorted({row["scope_id"] for row in active})
    narrowed = dict(params, scopes=hit_scopes or None)

    cur.execute(_LAYER2_SQL, dict(narrowed, limit=max(limit, 1) * 4))
    unreviewed, dropped = _cap_layer2(cur.fetchall(), len(active))
    for row in unreviewed:
        row["tag"] = UNREVIEWED_TAG

    retired: list[dict[str, Any]] = []
    if hit_scopes:
        cur.execute(_LAYER3_SQL, narrowed)
        retired = sorted(cur.fetchall(), key=lambda r: -r["similarity"])[:limit]

    result = Retrieved(
        query=query,
        scopes=list(scopes),
        narrowed_to=list(hit_scopes),
        active=active,
        unreviewed=unreviewed,
        retired=retired,
        dropped_unreviewed=dropped,
    )
    if record:
        events.record(
            cur,
            EventType.CONTEXT_ASSEMBLED,
            actor,
            detail={
                "query": query,
                "scopes": [str(s) for s in result.scopes],
                "narrowed_to": [str(s) for s in result.narrowed_to],
                "layer1": [str(r["memory_id"]) for r in active],
                "layer2": [str(r["memory_id"]) for r in unreviewed],
                "layer3": [str(r["memory_id"]) for r in retired],
                "dropped_unreviewed": dropped,
            },
        )
    return result


def _cap_layer2(rows: list[dict], active_count: int) -> tuple[list[dict], int]:
    """Apply both caps of 21.1, keeping the closest matches.

    The relative cap is applied first because it is the one with a reason
    beyond budget. When nothing at all is active the layer is emptied: there is
    then no untagged material for the tag to contrast against, and a context
    built entirely of unreviewed content is exactly what the cap exists to
    prevent.
    """
    kept: list[dict] = []
    spent = 0
    for row in rows[:active_count]:
        cost = estimate_tokens(row["content"])
        if spent + cost > LAYER2_TOKEN_BUDGET and kept:
            break
        kept.append(row)
        spent += cost
    return kept, len(rows) - len(kept)


def _as_vector(values: list[float]) -> str:
    """pgvector's text input form."""
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"
