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
from mashu.models import EventType, MemoryType, VersionStatus

#: How many active memories one query returns before layer 2 is sized.
DEFAULT_LIMIT = 8

#: Absolute cap on layer 2, in tokens (21.1). Provisional; 27.5 revisits it.
LAYER2_TOKEN_BUDGET = 1500

#: The same for layer 1, which had none.
#:
#: The row count was capped and the row size was not, so one long memory
#: could carry a whole answer past ten thousand token. Measured on the real
#: store, one query returned 10,917 token of which a single row was 1,444.
#: A query whose subject the store knows nothing about is the expensive
#: case, because the embeddings are anisotropic and eight rows come back
#: regardless — so the cost was highest exactly where the value was lowest.
#:
#: What is over the line keeps its title and its id and loses its content,
#: the same trade 21.2 makes at the session opening: a reader missing a body
#: can fetch it, a reader missing the row does not know it exists.
LAYER1_TOKEN_BUDGET = 4000

#: What scope detection reads, and how it weighs what it reads (27.2, 30 段 D).
#:
#: Two decisions, taken from measurement, and they answer different questions.
#:
#: **What is probed.** The probe covers everything the layers can hand over, so
#: adopted versions and unreviewed candidates alike. Probing only what has been
#: adopted puts back, one floor down, the rule v0.11 withdrew: a scope holding
#: nothing adopted is then invisible to detection, invisible to layer 1, and
#: therefore absent from the scopes layers 2 and 3 are confined to — its
#: candidates cannot be reached by any query at all. Measured on the real
#: ledger, one scope of four was in exactly that state and none of its 37
#: memories could be retrieved. Review confirms quality; it does not grant use,
#: and that has to hold in the reading path as well as in the cap.
#:
#: **How the probe votes.** A hit's vote decays with how far it sits below the
#: best hit. A flat floor with one vote per hit counts rank 25 as loudly as
#: rank 1, which loses whenever a scope holds a lot of near-miss material: the
#: query about a monologue giving away a key exhibit put its two best matches
#: in the right scope and then lost to thirteen mid-range matches from another.
#:
#: The floor that used to select the votes is now a gate on the best hit alone,
#: which is the one question an absolute similarity can answer here — did this
#: query match anything at all. It still earns its place: three of five queries
#: about subjects the ledger has nothing on are turned away by it.
#:
#: Why an absolute floor cannot do more than that: the embeddings are strongly
#: anisotropic. The mean of the 242 stored vectors has norm 0.904, so nine
#: tenths of every unit vector is a direction they all share, and unrelated
#: documents sit at cosine 0.815 with a spread of 0.032. Every similarity this
#: model reports is therefore that shared component plus a small residue, and a
#: cutoff placed inside the residue moves as the ledger grows. It did: the
#: values 27.2 measured no longer separate the same queries.
#:
#: Concentration is measured as lift — the share of the decayed weight a scope
#: takes, divided by the share of the ledger it holds — and not as a raw share.
#: A raw share has no size invariance, and capture makes that fatal rather than
#: theoretical: routes point at the directories actually worked in, so one
#: scope grows to hold most of the ledger and then wins every query by mass.
#: A ratio asks the only question worth asking: does this scope answer better
#: than its size would predict?
#:
#: SCOPE_SHARE is what decides, and SCOPE_LIFT is the guard beside it. Requiring
#: three fifths of the weight means at most one scope is ever detected, which is
#: the shape the measurement supports; a query genuinely spanning two scopes is
#: better served by the wide search than by a narrowing that keeps one of them.
#:
#: SCOPE_LIFT stays at the value 27.2 measured. It sets the size a scope can
#: reach before it becomes undetectable — 1 / SCOPE_LIFT of the ledger, so 83%
#: here — and that ceiling is the mechanism degrading in the right direction:
#: narrowing to a scope that is nearly the whole store buys nothing. Raising it
#: to 2.0 scores marginally better on the current queries and moves the ceiling
#: to 50%, which the largest scope is already within sight of at 42%.
#:
#: Measured over 18 queries against the real ledger: 10 narrowed to a right
#: scope alone — one of them to the second of two, since the ledger holds that
#: discipline both as a general rule and as a project's instance of it — 3
#: declined and so searched everything, 5 of 5 queries about subjects the
#: ledger has nothing on were turned away, and none was narrowed to a wrong
#: scope. The mechanism this replaces got 5 right and 7 wrong on the same
#: queries. `mashu admin thresholds` runs the measurement.
SCOPE_PROBE = 25
SCOPE_LIFT = 1.2
SCOPE_SHARE = 0.6
#: How fast a vote decays below the best hit, in cosine units. Calibrated on
#: multilingual-e5-large, where the whole probe spans about 0.03.
SCOPE_DECAY = 0.008
SCOPE_MATCH_THRESHOLDS = {
    "intfloat/multilingual-e5-large": 0.82,
    "hashing": 0.50,
}
SCOPE_MATCH_FALLBACK = 0.78
SCOPE_THRESHOLD_ENV_VAR = "MASHU_SCOPE_THRESHOLD"

UNREVIEWED_TAG = "unreviewed"

#: What is added to a layer 1 row whose own version says the thing is over.
#:
#: A completion keeps the active pointer, because "this was done" is worth
#: finding and is the answer to asking about it again. What it must not do is
#: arrive looking like work still outstanding. Until this tag existed a task
#: retired as completed came back first in layer 1, full content, with nothing
#: on it: the store held the completion, said so in the version's own reason,
#: and handed the reader the opposite.
FINISHED_TAG = "finished"

#: What is added to a layer 1 row somebody has proposed retiring (21.1, 30 段 B).
RETIREMENT_PROPOSED_TAG = "retirement_proposed"

#: What is added to a layer 1 row somebody has already rewritten (21.1). The
#: half of that section's annotation that had not been built: a newer reading
#: exists and is waiting for a person, and until they get to it the reader is
#: being handed the older one with nothing to say so.
UPDATE_PROPOSED_TAG = "update_proposed"

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
    #: Layer 1 rows whose content was dropped to stay inside the budget.
    #: Still listed, by title, so memory_get can fetch what was cut.
    shortened: list[UUID] = field(default_factory=list)
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
# The set the probe reads: layer 1's rows and layer 2's rows together, which is
# everything a query can be answered with. The two halves are the WHERE clauses
# of _LAYER1_SQL and _LAYER2_SQL, held in one place so they cannot drift apart
# from what the layers actually hand over.
_HANDED_OVER = """
    (e.status = 'active' AND v.version_id = e.active_version)
 OR (e.status IN ('active', 'provisional') AND v.status = 'candidate'
     AND (e.active_version IS NULL OR e.active_version <> v.version_id))
"""

_SCOPE_SIZE_SQL = f"""
SELECT e.scope_id, count(*) AS held
FROM memory_entity e
JOIN memory_version v ON v.memory_id = e.memory_id
WHERE ({_HANDED_OVER})
GROUP BY e.scope_id
"""

_SCOPE_PROBE_SQL = f"""
SELECT e.scope_id, 1 - (v.content_embedding <=> %(q)s::vector) AS similarity
FROM memory_entity e
JOIN memory_version v ON v.memory_id = e.memory_id
WHERE v.content_embedding IS NOT NULL
  AND ({_HANDED_OVER})
ORDER BY v.content_embedding <=> %(q)s::vector
LIMIT %(probe)s
"""


@dataclass
class ScopeReading:
    """What the probe saw, in the two forms the caller needs.

    Detection is a claim; the probe is an observation. They are returned apart
    because a query that detects nothing has still been told where its best
    matches live, and layers 2 and 3 need somewhere to stand that is not "the
    scopes layer 1 happened to hit" — that set can never contain a scope whose
    every memory is still a candidate.
    """

    #: Scopes concentrated enough to narrow to. At most one, by SCOPE_SHARE.
    detected: list[UUID]
    #: Scopes the gated probe hits live in, best match first. Empty when the
    #: query matched nothing anywhere.
    probed: list[UUID]


def detect_scopes(cur: psycopg.Cursor, query_vector: str) -> ScopeReading:
    """Which scopes the query is about, judged by what they hold (21, 27.2).

    The probe is the best few dozen memories across the whole ledger, adopted
    and unreviewed alike. A scope is detected when it takes most of that
    probe's weight and takes more of it than its size predicts: a claim about
    concentration, which survives every similarity in the set sitting inside a
    tenth of each other, and the only thing the measurement showed these
    embeddings can support.

    Returning nothing detected means no scope stood out, and the caller
    searches everything. That stays the safe direction and stays the default:
    guessing one scope and being wrong hides the answer completely, while a
    wider search only costs ranking.
    """
    gate = float(
        os.environ.get(SCOPE_THRESHOLD_ENV_VAR)
        or SCOPE_MATCH_THRESHOLDS.get(get_embedder().name, SCOPE_MATCH_FALLBACK)
    )
    cur.execute(_SCOPE_PROBE_SQL, {"q": query_vector, "probe": SCOPE_PROBE})
    hits = cur.fetchall()
    if not hits or hits[0]["similarity"] < gate:
        return ScopeReading(detected=[], probed=[])

    best_overall = hits[0]["similarity"]
    weight: dict[UUID, float] = {}
    best: dict[UUID, float] = {}
    for row in hits:
        scope, similarity = row["scope_id"], row["similarity"]
        weight[scope] = weight.get(scope, 0.0) + math.exp(
            -(best_overall - similarity) / SCOPE_DECAY
        )
        best[scope] = max(best.get(scope, 0.0), similarity)
    probed = sorted(weight, key=lambda s: -best[s])

    cur.execute(_SCOPE_SIZE_SQL)
    held = {row["scope_id"]: row["held"] for row in cur.fetchall()}
    total = sum(held.values())
    mass = sum(weight.values())
    if not total or not mass:
        return ScopeReading(detected=[], probed=probed)

    detected = []
    for scope, own in weight.items():
        share = own / mass
        expected = held.get(scope, 0) / total
        if share >= SCOPE_SHARE and expected and share / expected >= SCOPE_LIFT:
            detected.append(scope)

    return ScopeReading(detected=detected, probed=probed)


# --------------------------------------------------------------------------
# the layers
# --------------------------------------------------------------------------
# A completion keeps the active pointer on purpose (11), so that a finished
# task answers searches with its finish shown rather than disappearing. That is
# right for the reading path and wrong here: this set exists so extraction can
# ask "what has been overtaken", and something already retired is not a
# question. Left in, every completed task is re-read and re-paid on every
# extraction of its scope for ever.
ACTIVE_SET_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, v.version_id, v.content,
       v.created_at
FROM memory_entity e
JOIN memory_version v ON v.version_id = e.active_version AND v.memory_id = e.memory_id
WHERE e.status = 'active'
  AND v.status = 'candidate'
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
       v.status AS version_status, v.reason AS version_reason,
       1 - (v.content_embedding <=> %(q)s::vector) AS similarity,
       r.proposed_status, r.proposed_reason, r.proposed_by,
       u.update_proposed_by, u.update_proposed_days
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
LEFT JOIN LATERAL (
    SELECT p.actor AS update_proposed_by,
           EXTRACT(DAY FROM now() - p.created_at)::int AS update_proposed_days
    FROM proposal p
    WHERE p.target_memory = e.memory_id
      AND p.status = 'pending'
      AND p.operation = 'update_version'
    ORDER BY p.seq DESC
    LIMIT 1
) u ON TRUE
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

    if scope_id is not None:
        reading = ScopeReading(detected=[scope_id], probed=[scope_id])
    else:
        reading = detect_scopes(cur, q)
    scopes = reading.detected
    params = {
        "q": q,
        "scopes": scopes or None,
        "types": type_filter,
        "limit": limit,
    }

    cur.execute(_LAYER1_SQL, params)
    active = cur.fetchall()
    for row in active:
        # Settled before proposed: a version that already carries a retirement
        # is not a question anyone is still asking.
        if row.get("version_status") and row["version_status"] != str(VersionStatus.CANDIDATE):
            row["tag"] = FINISHED_TAG
        elif row.get("proposed_status"):
            row["tag"] = RETIREMENT_PROPOSED_TAG
        elif row.get("update_proposed_by"):
            row["tag"] = UPDATE_PROPOSED_TAG

    # Layers 2 and 3 stay inside the scopes the query landed in. Returning a
    # whole scope's retired memories would let layer 3 crowd the context out
    # (21.1, MVP default).
    #
    # When detection declines, that set is where the probe's best matches live
    # rather than where layer 1's rows live. The two differ in exactly the case
    # that matters: layer 1 reads adopted versions only, so a scope holding
    # nothing but candidates can never appear among its rows, and confining
    # layer 2 to them would hide that scope's candidates from every query — the
    # rule v0.11 withdrew, reappearing as a side effect of the narrowing.
    #
    # Layer 1's scopes remain the last resort, for a query that matched nothing
    # anywhere and so has no probe to stand on. With nothing to narrow by the
    # narrowing does not happen: an empty scope list means the whole ledger
    # here, not nothing. Reading it as nothing would silence layer 3 in the one
    # case where it is the entire answer, which is a scope whose every memory
    # has been retired. Scenario 1 is that case on day 10.
    hit_scopes = scopes or reading.probed or sorted({row["scope_id"] for row in active})
    narrowed = dict(params, scopes=hit_scopes or None)

    shortened = _cap_layer1(active)

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
        shortened=shortened,
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
                "shortened": [str(m) for m in shortened],
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


def _cap_layer1(rows: list[dict]) -> list[UUID]:
    """Drop the content of what is over the budget, in place (21.1).

    Ordered by rank, so the closest matches keep their bodies and the tail
    gives them up. The first row is admitted whatever it costs, for the reason
    _cap_layer2 gives: nothing at all is a worse answer than one long one.
    """
    spent = 0
    shortened: list[UUID] = []
    for row in rows:
        cost = estimate_tokens(row["content"] or "")
        if spent + cost > LAYER1_TOKEN_BUDGET and spent:
            row["content"] = None
            row["shortened"] = True
            shortened.append(row["memory_id"])
            continue
        spent += cost
    return shortened


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
