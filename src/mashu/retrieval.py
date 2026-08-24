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

Layer 2 is capped once, by a token budget (21.1). It used to be capped twice:
a relative cap kept it no larger than layer 1, on the grounds that a tag only
changes behaviour while untagged material sits beside it. v0.11 withdrew that,
because it also emptied the layer whenever nothing was adopted, which made
review the gate to use rather than the confirmation of quality. What it
protected is measured afterwards as the tag ratio (27.3) instead of enforced.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from mashu import context, events
from mashu.embed import get_embedder
from mashu.models import EventType, MemoryType

#: How many active memories one query returns before layer 2 is sized.
DEFAULT_LIMIT = 8

#: Absolute cap on layer 2, in tokens (21.1). Provisional; 27.5 revisits it.
LAYER2_TOKEN_BUDGET = 1500

#: What scope detection needs, now that it works on held memories rather than
#: on labels (27.2, 30 段 D).
#:
#: The old mechanism matched the query against each scope's name and one-line
#: description, and the measurement in 27.2 found it does not work: against the
#: real ledger every query scored between 0.72 and 0.81 against every scope, and
#: the ranking was wrong on four of seven queries — a question about delegation
#: ranked two unrelated scopes above the one holding the delegation rules, and a
#: question about the weather scored 0.759, inside the range the real matches
#: occupy. That is a mechanism failing, not a constant needing a nudge: a scope
#: name is three words, and three words do not carry what a scope is about.
#:
#: What does carry it is what the scope holds. So the query is run against the
#: memories themselves — the same index layer 1 already ranks well with — and
#: the scopes those best matches live in are the answer. It becomes a question
#: about relative order, which the embeddings answer, instead of a question
#: about an absolute similarity, which they do not.
#:
#: The floor stays, in a much smaller role: a query matching nothing anywhere
#: must not "detect" the scope holding the least unrelated memory. It earns its
#: place — at 0.78 the weather question concentrates 8 of its 10 hits in one
#: scope and gets confidently narrowed to it.
#:
#: Measured over the same seven queries and the same ledger 27.2 used, at the
#: floor and lift below: four narrowed to the right scope alone, three detected
#: nothing and so searched everything. **No query was narrowed to a wrong
#: scope**, which is the failure that hides an answer outright and the one the
#: old mechanism made four times out of seven. A tighter floor (0.84) detects
#: nothing on six of seven; a looser one (0.78) starts narrowing questions that
#: match nothing — the weather question concentrates 8 of its 10 hits in one
#: scope at x1.49.
#:
#: Two of the three that decline are right to. One asks about the scope holding
#: two memories, which cannot show concentration however well it matches. The
#: other is about a subject the ledger has nothing on: its hits split x0.99 /
#: x1.08, which is what "no scope answers this better than its size predicts"
#: looks like. The share-based version this replaced narrowed that one to two
#: scopes.
#: Concentration is measured as lift, not as a raw share: what fraction of the
#: best matches a scope holds, divided by what fraction of the store it holds.
#:
#: A raw share has no size invariance, and capture makes that fatal rather than
#: theoretical — routes point at the directories actually worked in, so one
#: scope grows to hold most of the ledger and then wins every query by mass. A
#: ratio asks the only question worth asking: does this scope answer better
#: than its size would predict?
#:
#: It degrades the right way at the extreme. A scope holding almost everything
#: can hardly ever clear the ratio, so it is never "detected" — and narrowing
#: to a scope that is nearly the whole store buys nothing anyway.
SCOPE_PROBE = 25
SCOPE_MIN_HITS = 2
SCOPE_LIFT = 1.2
SCOPE_MATCH_THRESHOLDS = {
    "intfloat/multilingual-e5-large": 0.82,
    "hashing": 0.50,
}
SCOPE_MATCH_FALLBACK = 0.78
SCOPE_THRESHOLD_ENV_VAR = "MASHU_SCOPE_THRESHOLD"

UNREVIEWED_TAG = "unreviewed"

#: What is added to a layer 1 row somebody has proposed retiring (21.1, 30 段 B).
RETIREMENT_PROPOSED_TAG = "retirement_proposed"

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
    #: Candidates that matched but were not handed over, counted across the
    #: whole matching set rather than across the query window.
    dropped_unreviewed: int = 0
    #: Conditions that apply right now (25.2). Beside the three layers rather
    #: than a fourth one: the layers sort indefinite knowledge by its standing,
    #: and a condition with a clock on it is outside that sorting. Included
    #: whoever wrote it — section 25.2 separates the right to be pushed into
    #: every session from the right to be findable, and this is the second.
    temporary: list[dict[str, Any]] = field(default_factory=list)

    def memory_ids(self) -> list[UUID]:
        """Every memory handed over, for the event log (22)."""
        return [row["memory_id"] for row in (*self.active, *self.unreviewed, *self.retired)]


# --------------------------------------------------------------------------
# scope detection
# --------------------------------------------------------------------------
_SCOPE_SIZE_SQL = """
SELECT e.scope_id, count(*) AS held
FROM memory_entity e
WHERE e.status = 'active' AND e.active_version IS NOT NULL
GROUP BY e.scope_id
"""

_SCOPE_PROBE_SQL = """
SELECT e.scope_id, 1 - (v.content_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version AND v.memory_id = e.memory_id
WHERE e.status = 'active'
  AND v.content_embedding IS NOT NULL
ORDER BY v.content_embedding <=> %(q)s::vector
LIMIT %(probe)s
"""


def detect_scopes(cur: psycopg.Cursor, query_vector: str) -> list[UUID]:
    """Which scopes the query is about, judged by what they hold (21, 27.2).

    The probe is the top few dozen active memories across the whole ledger. A
    scope is detected when a real share of those best matches live in it: that
    is a claim about concentration, which survives every similarity in the set
    sitting inside a tenth of each other, and it is the only thing the
    measurement showed these embeddings can actually support.

    Returning nothing means no scope stood out, and the caller searches
    everything. That stays the safe direction and stays the default: guessing
    one scope and being wrong hides the answer completely, while a wider search
    only costs ranking.
    """
    floor = float(
        os.environ.get(SCOPE_THRESHOLD_ENV_VAR)
        or SCOPE_MATCH_THRESHOLDS.get(get_embedder().name, SCOPE_MATCH_FALLBACK)
    )
    cur.execute(_SCOPE_PROBE_SQL, {"q": query_vector, "probe": SCOPE_PROBE})
    hits = [row for row in cur.fetchall() if row["similarity"] >= floor]
    if len(hits) < SCOPE_MIN_HITS:
        return []

    cur.execute(_SCOPE_SIZE_SQL)
    held = {row["scope_id"]: row["held"] for row in cur.fetchall()}
    total = sum(held.values())
    if not total:
        return []

    counts: dict[UUID, int] = {}
    best: dict[UUID, float] = {}
    for row in hits:
        counts[row["scope_id"]] = counts.get(row["scope_id"], 0) + 1
        best[row["scope_id"]] = max(best.get(row["scope_id"], 0.0), row["similarity"])

    detected = []
    for scope, count in counts.items():
        if count < SCOPE_MIN_HITS:
            continue
        expected = held.get(scope, 0) / total
        if expected and (count / len(hits)) / expected >= SCOPE_LIFT:
            detected.append(scope)

    # Ordered by how well the scope's own best memory answered, so a caller
    # that takes only the first takes the strongest rather than an arbitrary one.
    return sorted(detected, key=lambda s: -best[s])


# --------------------------------------------------------------------------
# the layers
# --------------------------------------------------------------------------
ACTIVE_SET_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.content,
       v.created_at
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version AND v.memory_id = e.memory_id
WHERE e.status = 'active'
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
ORDER BY e.type, e.title
"""


def active_set(cur: psycopg.Cursor, *, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """Everything a scope currently holds as true, whole (16.1).

    The retirement half of session end extraction cannot work from a query.
    Asking what has been overtaken means reading every standing memory against
    what the session observed, and a memory nobody thought to search for is
    exactly the one that goes on being wrong. So this is the one read with no
    ranking, no limit and no caps.

    It answers with the same set layer 1 draws from, provisional entities
    excluded, because an entity still waiting to be told apart from another is
    not yet something the scope holds as true.
    """
    cur.execute(ACTIVE_SET_SQL, {"scopes": [scope_id] if scope_id else None})
    return cur.fetchall()


# The annotation on layer 1 is section 30 段 B's third case. An agent-inferred
# retirement does not take effect on its own — nobody would notice a wrong one,
# because what is gone does not appear in searches to be argued with. But
# handing back a memory somebody has proposed retiring as though nothing were
# in question is the other half of the same mistake, so the proposal rides
# along with the content and the reader decides.
_LAYER1_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.content,
       1 - (v.content_embedding <=> %(q)s::vector) AS similarity,
       r.proposed_status, r.proposed_reason, r.proposed_by
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version AND v.memory_id = e.memory_id
LEFT JOIN LATERAL (
    SELECT p.payload ->> 'status' AS proposed_status,
           p.payload ->> 'reason' AS proposed_reason,
           p.actor                AS proposed_by
    FROM proposal p
    WHERE p.target_memory = e.memory_id
      AND p.status = 'pending'
      AND p.operation = 'change_status'
      AND p.payload ->> 'status' IN ('disproven', 'dormant', 'completed')
    ORDER BY p.seq DESC
    LIMIT 1
) r ON TRUE
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
#
# rejected belongs here alongside the two retired readings even though it is
# not itself a reading. What the layer is for is telling the agent not to
# re-derive something, and "a reviewer turned this down, here is why" is that
# same instruction; the status the agent reads back keeps the two apart.
# What the layer 2 window did not even look at. dropped_unreviewed is read as
# "how much was held back", so counting only the rows the window happened to
# fetch turns it into a number that shrinks as the backlog grows.
_LAYER2_TOTAL_SQL = """
SELECT count(*) AS n
FROM memory_entity e
JOIN memory_version v ON v.memory_id = e.memory_id
WHERE e.status IN ('active', 'provisional')
  AND v.status = 'candidate'
  AND (e.active_version IS NULL OR e.active_version <> v.version_id)
  AND v.content_embedding IS NOT NULL
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
  AND (%(types)s::text[] IS NULL OR e.type = ANY(%(types)s::text[]))
"""

_LAYER3_SQL = """
SELECT DISTINCT ON (e.memory_id)
       e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.status, v.reason,
       1 - (e.title_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity e
JOIN memory_version v ON v.memory_id = e.memory_id
WHERE v.status IN ('disproven', 'dormant', 'rejected')
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

    scopes = [scope_id] if scope_id is not None else detect_scopes(cur, q)
    params = {
        "q": q,
        "scopes": scopes or None,
        "types": type_filter,
        "limit": limit,
    }

    cur.execute(_LAYER1_SQL, params)
    active = cur.fetchall()
    for row in active:
        if row.get("proposed_status"):
            row["tag"] = RETIREMENT_PROPOSED_TAG

    # Layers 2 and 3 stay inside the scopes layer 1 actually hit. Returning a
    # whole scope's retired memories would let layer 3 crowd the context out
    # (21.1, MVP default).
    #
    # With nothing to narrow by the narrowing does not happen, exactly as when
    # scope detection comes up empty: an empty scope list means the whole
    # ledger here, not nothing. Reading it as nothing would silence layer 3 in
    # the one case where it is the entire answer, which is a scope whose every
    # memory has been retired. Scenario 1 is that case on day 10, and it only
    # passed because it hands the scope in.
    hit_scopes = scopes or sorted({row["scope_id"] for row in active})
    narrowed = dict(params, scopes=hit_scopes or None)

    cur.execute(_LAYER2_SQL, dict(narrowed, limit=LAYER2_ROW_WINDOW))
    rows = cur.fetchall()
    unreviewed, _ = _cap_layer2(rows)
    for row in unreviewed:
        row["tag"] = UNREVIEWED_TAG

    # The count comes from the whole matching set, not from the window, so a
    # backlog larger than the window is reported at its real size.
    cur.execute(_LAYER2_TOTAL_SQL, narrowed)
    dropped = cur.fetchone()["n"] - len(unreviewed)

    cur.execute(_LAYER3_SQL, narrowed)
    retired = sorted(cur.fetchall(), key=lambda r: -r["similarity"])[:limit]

    temporary = context.live(cur, scopes=list(hit_scopes) or None, limit=limit)

    result = Retrieved(
        query=query,
        scopes=list(scopes),
        narrowed_to=list(hit_scopes),
        active=active,
        unreviewed=unreviewed,
        retired=retired,
        dropped_unreviewed=dropped,
        temporary=temporary,
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


def preview(
    cur: psycopg.Cursor,
    query: str,
    *,
    scope_id: UUID,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Rank a scope's candidates for a query, with none of the caps applied.

    This is not context and never becomes context. Retrieval withholds the
    unreviewed layer when there is nothing adopted to contrast it against
    (21.1), which is right for an agent and useless for the one question 27.1
    asks of a freshly migrated scope: does the query find the right memory.
    Answering that needs to see the ranking the caps hide, so it is a separate
    entry point rather than a flag on the one agents call — a flag would be a
    way to ask retrieval to drop its guard.

    Nothing is logged as context assembly, because nothing was assembled.
    """
    embedder = get_embedder()
    cur.execute(
        _LAYER2_SQL,
        {
            "q": _as_vector(embedder.embed_query(query)),
            "scopes": [scope_id],
            "types": None,
            "limit": limit,
        },
    )
    return cur.fetchall()


#: How many candidate rows are fetched before the token cap is applied. It used
#: to be four times the caller's limit, which was sized for the relative cap
#: withdrawn in v0.11. It is a fetch bound, not a rule: at 1500 token the cap
#: binds first unless every row is shorter than about twenty token, and the
#: dropped count is taken from the whole matching set either way, so a backlog
#: larger than this is still reported at its real size.
LAYER2_ROW_WINDOW = 64


def _cap_layer2(rows: list[dict]) -> tuple[list[dict], int]:
    """Apply the token cap of 21.1, keeping the closest matches.

    The relative cap that used to run first — layer 2 holding no more rows than
    layer 1 — was withdrawn in v0.11. It emptied the layer whenever nothing was
    active, which meant a scope with nothing reviewed answered nothing, and
    that is the rule v0.7 said it was removing: review confirms quality, it does
    not grant use. Keeping it here put the same rule back one floor down.

    What it protected has not been abandoned. The tag needs untagged material
    to contrast against, and that is now measured rather than enforced (27.3).
    The trade was taken knowingly: an invariant became an observation, so the
    failure is noticed afterwards instead of prevented.

    A first row over the whole budget is still admitted. Returning nothing at
    all to a query that matched something is worse than returning one long
    answer, and the caller can see the cost.
    """
    kept: list[dict] = []
    spent = 0
    for row in rows:
        cost = estimate_tokens(row["content"])
        if spent + cost > LAYER2_TOKEN_BUDGET and kept:
            break
        kept.append(row)
        spent += cost
    return kept, len(rows) - len(kept)


def _as_vector(values: list[float]) -> str:
    """pgvector's text input form."""
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"
