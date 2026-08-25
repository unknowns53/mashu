"""Session Bootstrap: the context handed over before any query (specification 21.2).

Retrieval is query-driven, so it answers nothing until an agent asks. An agent
that does not know what the store holds has no reason to ask, which is why
section 6.1 calls the pull path dead without this. Bootstrap is the push half:
a fixed payload delivered at session start whether the agent asks or not.

Three parts, and the third is the one that matters most. The pushed memories
are knowledge; the scope index is a *map* of the knowledge, and it is the map
that gives every later memory_search a motive.

What gets pushed is decided by delivery and not by type. The first version of
this pushed every active preference and every current state, which made a
fixed per-session cost a function of how much the store held: measured against
the real migration it wanted 45,840 token against a 2,000 ceiling. Being a
preference says what a memory is, not that every session needs it in front of
it, and only the second is a reason to spend the session's opening budget.

Nothing here embeds anything. Bootstrap has no query to encode, so it reads
rows and counts characters and never touches the model. Session start is the
one moment where a cold model load would be paid by every session, and it is
also the one moment with nothing to encode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg

from mashu import context, proposals, runs
from mashu.events import record
from mashu.models import Delivery, EventType
from mashu.retrieval import estimate_tokens

#: Section 21.2. Bootstrap is a fixed cost paid by every session, so without a
#: ceiling the budget grows with the store. Re-measured by the switchover trial
#: (27.5), which is the first time the real per-session cost is observable.
BOOTSTRAP_TOKEN_BUDGET = 2000

# The index is the map: what it costs is bounded by the number of scopes, and
# it is what makes everything else reachable by name. An agent missing a body
# can fetch it; an agent missing the index does not know there is anything to
# fetch (21.2).
_SCOPE_INDEX_SQL = """
SELECT scope_id, name, description
FROM scope
WHERE status = 'active'
ORDER BY name
"""

# The payload query reads the pointer, never the version status: active is
# what active_version points at and nowhere else (specification 10).
#
# coalesce is where the short form earns its keep. directive is the standing
# rule as a person reviewed it; content is the whole of it with the reasons.
# The push takes the first, the pull takes the second, so keeping the session
# opening small no longer costs the reasons.
#
# The notes on a pending change ride along here too (21.1). This is the most
# privileged read there is — pushed into every session before it asks for
# anything — so it is the last place that should hand over a memory somebody
# has argued is finished, or has already rewritten, without saying so.
#
# The update note matters most exactly here. What is pushed is the adopted
# reading, and a Current State goes out of date in hours while a review takes
# days; the session that most needs to know a newer one is waiting is the one
# being handed the older one before it has asked anything.
_PUSHED_SQL = """
SELECT e.memory_id, e.scope_id, e.type, e.title, e.delivery,
       v.version_id, coalesce(v.directive, v.content) AS content,
       v.directive IS NOT NULL AS shortened,
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
  AND e.delivery = %(delivery)s
  AND (%(scopes)s::uuid[] IS NULL OR e.scope_id = ANY(%(scopes)s::uuid[]))
ORDER BY e.title
"""


@dataclass
class Bootstrapped:
    """What a session is handed before it asks anything."""

    scope_index: list[dict[str, Any]] = field(default_factory=list)
    #: Pushed before the session knows anything about itself.
    startup: list[dict[str, Any]] = field(default_factory=list)
    #: Pushed only for the scopes the session named.
    scoped: list[dict[str, Any]] = field(default_factory=list)
    #: What applies until a stated moment (25.2). Beside the three layers
    #: rather than inside them: these are the conditions of the moment, not
    #: knowledge, and they leave by the clock rather than by a judgement.
    temporary: list[dict[str, Any]] = field(default_factory=list)
    #: Whether automatic capture is still working (16.3). It rides here because
    #: during a week nobody attends, the next session is the only reader
    #: guaranteed to arrive.
    health: dict[str, Any] = field(default_factory=dict)
    #: What is waiting for a person to decide. Beside health rather than in it:
    #: capture failing means nothing new arrives, review being behind means
    #: what arrived is less certain than it could be. Two different repairs.
    review: dict[str, Any] = field(default_factory=dict)
    #: What is already held and is due to be checked again (13.1, 30 段 B).
    #: The third of the same kind, and the one that was missing: the sweep for
    #: memories past their shelf life had no way of reaching anybody, so it was
    #: found by noticing the store had gone wrong rather than by being told.
    upkeep: dict[str, Any] = field(default_factory=dict)
    #: Memory IDs whose content was dropped to stay inside the budget. They are
    #: still listed, by title, so memory_get can fetch what was cut.
    trimmed: list[UUID] = field(default_factory=list)
    #: What the payload costs as handed over, after any trimming. The
    #: pre-trim figure is not the session's cost and would not be the
    #: number 27.5 needs to re-measure the ceiling against.
    tokens: int = 0
    #: True when trimming ran out of content to drop and the payload is still
    #: over the ceiling. The scope index is never trimmed (21.2), so an index
    #: that alone exceeds the budget has no way down; saying so is the least a
    #: fixed cost can do when it stops being fixed.
    over_budget: bool = False

    def memory_ids(self) -> list[UUID]:
        """Everything named in the payload, trimmed or whole."""
        return [row["memory_id"] for row in (*self.startup, *self.scoped)]


def session_bootstrap(
    cur: psycopg.Cursor,
    *,
    actor: str,
    scopes: list[UUID] | None = None,
    budget: int = BOOTSTRAP_TOKEN_BUDGET,
    record_event: bool = True,
) -> Bootstrapped:
    """Assemble the fixed session-start context and log what was handed over.

    scopes narrows the current state to the scopes a session already knows it
    is working in. Leaving it out is the ordinary case: a session that has not
    started cannot know its scope yet, and the index is what tells it.
    """
    from mashu import metrics  # imports this module, so not at the top

    cur.execute(_SCOPE_INDEX_SQL)
    scope_index = [
        {
            "scope_id": row["scope_id"],
            "name": row["name"],
            "summary": _one_line(row["description"]),
        }
        for row in cur.fetchall()
    ]

    # startup_required ignores the scope filter: it is what a session needs
    # before it knows which scope it is in, so confining it to a scope the
    # session has not identified yet would push nothing at all.
    cur.execute(_PUSHED_SQL, {"scopes": None, "delivery": str(Delivery.STARTUP_REQUIRED)})
    startup = cur.fetchall()
    scoped: list[dict[str, Any]] = []
    if scopes:
        cur.execute(_PUSHED_SQL, {"scopes": scopes, "delivery": str(Delivery.SCOPE_REQUIRED)})
        scoped = cur.fetchall()

    # 25.2: what applies right now, and only what a person put there. An agent
    # may write a window but may not have it pushed: a session start reaches
    # every session including the ones that never asked about it.
    temporary = context.live(
        cur, scopes=scopes, pushed_only=True, limit=context.BOOTSTRAP_ITEM_LIMIT
    )

    trimmed = _fit(scope_index, scoped, startup, budget=budget)
    startup = [_shape(row) for row in startup]
    scoped = [_shape(row) for row in scoped]
    result = Bootstrapped(
        scope_index=scope_index,
        startup=startup,
        scoped=scoped,
        temporary=temporary,
        health=runs.health(cur),
        review=proposals.backlog(cur),
        upkeep=metrics.upkeep(cur),
        trimmed=trimmed,
        tokens=_total_tokens(scope_index, startup, scoped),
    )
    result.over_budget = result.tokens > budget

    if record_event:
        record(
            cur,
            EventType.SESSION_BOOTSTRAPPED,
            actor,
            detail={
                "scopes": [str(s) for s in (scopes or [])],
                "scope_index": [str(row["scope_id"]) for row in scope_index],
                "memories": [str(m) for m in result.memory_ids()],
                "trimmed": [str(m) for m in trimmed],
                "tokens": result.tokens,
                "over_budget": result.over_budget,
                "temporary": [str(row["context_id"]) for row in temporary],
                "capture_ok": result.health.get("ok"),
                "review_ok": result.review.get("ok"),
                "upkeep_ok": result.upkeep.get("ok"),
            },
        )
    return result


def _one_line(description: str | None) -> str:
    """A scope's summary as one line. An empty description is not an error."""
    if not description:
        return ""
    return " ".join(description.split())


def _fit(
    scope_index: list[dict[str, Any]],
    first_to_give: list[dict[str, Any]],
    last_to_give: list[dict[str, Any]],
    *,
    budget: int,
) -> list[UUID]:
    """Drop content until the payload fits, in place. Returns what was cut.

    Section 21.2 keeps the whole scope index and spends the shortfall on the
    other two. The map is what makes the rest reachable: an agent missing a
    preference body can still fetch it by ID, while an agent missing the index
    does not know there is anything to fetch.

    Two orderings are chosen here that the section leaves open.

    Scope-required material gives up its content before startup-required
    material does. Something the session was going to be told before it knew
    anything about itself was judged to be needed by every session; something
    tied to one scope is needed by the work in that scope, and that work makes
    the gap obvious in a way a missing standing rule never does.

    That trimming should be rare now. Admission control (would_fit) refuses the
    change that would put the startup pack over the ceiling, so the trimming
    here is the backstop for the paths admission control does not sit on, not
    the ordinary way the budget is kept.

    Within a group the largest goes first, because cutting the biggest item
    buys the most budget per item lost, and the count of items still readable
    whole is what the trimming is trying to protect.
    """
    trimmed: list[UUID] = []
    total = _total_tokens(scope_index, first_to_give, last_to_give)
    if total <= budget:
        return trimmed

    for group in (first_to_give, last_to_give):
        for row in sorted(group, key=lambda r: -estimate_tokens(r["content"] or "")):
            if total <= budget:
                return trimmed
            total -= estimate_tokens(row["content"] or "")
            row["content"] = None
            trimmed.append(row["memory_id"])
    return trimmed


#: What a pushed row costs beyond its own words.
#:
#: Measured against the real payload rather than reasoned about. A row goes out
#: as JSON, and the keys and the UUIDs between them were not being counted. The
#: ceiling in 21.2 is an admission control that refuses a change putting the
#: opening over budget, so a counter reading half the true cost does not merely
#: misreport — it disables the invariant it exists to enforce. The store's own
#: opening measured 3,208 token on the wire against a counter saying 1,549.
#:
#: Counting it truthfully then showed what the scaffolding was: 96 token around
#: a sentence of 100, on a row of thirteen keys. See _shape, which is why this
#: is now a quarter of what it was. Measured across the store's own pack: 21
#: to 22 on a row carrying the short-form flag, 17 without it.
ROW_OVERHEAD = 22

#: The same for a scope in the index, which carries one UUID and three keys.
INDEX_OVERHEAD = 20

#: What rides along whatever else is pushed: capture, review, upkeep, the
#: standing note, and the envelope. Fixed, so it is added once.
ENVELOPE = 215


def _total_tokens(
    scope_index: list[dict[str, Any]],
    *groups: list[dict[str, Any]],
) -> int:
    """What the payload costs as it is actually handed over.

    Counted with the scaffolding, not only the words. See ROW_OVERHEAD.
    """
    cost = ENVELOPE
    cost += sum(
        estimate_tokens(f"{row['name']} {row['summary']}") + INDEX_OVERHEAD for row in scope_index
    )
    for group in groups:
        for row in group:
            cost += estimate_tokens(_line(row))
            cost += ROW_OVERHEAD
    return cost


def _line(row: dict[str, Any]) -> str:
    """The one line a pushed row hands over: its content, or its title instead.

    Counting both was counting something that is not sent. A row carries the
    directive, and only where that was trimmed away does the title go in its
    place — so the cost of a row is the cost of whichever of the two survives.
    """
    return row.get("content") or row.get("title") or ""


#: Notes on a pending change travel together or not at all. Either half of a
#: pair being present decides what the note says; sending one without the other
#: leaves the reader holding a claim with no reason attached to it.
_RETIREMENT_NOTE = ("proposed_status", "proposed_reason", "proposed_by")
_UPDATE_NOTE = ("update_proposed_by", "update_proposed_days")


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """One pushed row as it goes over the wire: only the keys that say anything.

    Thirteen keys were going out around one sentence — three UUIDs, two fields
    holding the same value on every row of the pack, and five nulls — costing
    96 token of scaffolding for 100 token of words. Two thirds of the opening
    was structure. Because the ceiling is admission control (21.2) rather than
    a bill, that does not merely make the session start dear: it spends the
    budget that decides which standing rules a session is told at all, and the
    store reached the point of refusing every promotion for a reason that had
    nothing to do with what was being promoted.

    The title goes where the content survives. Both were being sent and both
    said the same thing: a directive is the rule as a person reviewed it, and
    the titles here were written as whole sentences. Where the content was
    trimmed away the title is the only thing left to name the row by, so that
    is exactly where it stays.
    """
    shaped: dict[str, Any] = {"memory_id": row["memory_id"], "content": row["content"]}
    if row["content"] is None:
        shaped["title"] = row["title"]
    elif row["shortened"]:
        shaped["shortened"] = True
    if row.get("proposed_status"):
        shaped.update({key: row[key] for key in _RETIREMENT_NOTE})
    if row.get("update_proposed_by"):
        shaped.update({key: row[key] for key in _UPDATE_NOTE})
    return shaped


def _with_change(
    pack: list[dict[str, Any]],
    memory_id: UUID | None,
    content: str | None,
) -> list[dict[str, Any]]:
    """One opening, with the memory under consideration carrying its new body.

    Copies rather than edits in place: the same startup rows appear in every
    opening being weighed, so writing the hypothetical onto them would leave it
    on the rows the next opening is measured from.
    """
    if memory_id is None:
        return pack
    changed = [dict(row) for row in pack]
    for row in changed:
        if row.get("memory_id") == memory_id:
            row["content"] = content
            return changed
    return [*changed, {"title": "", "content": content}]


def would_fit(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID | None = None,
    content: str | None = None,
    scope_id: UUID | None = None,
    budget: int = BOOTSTRAP_TOKEN_BUDGET,
) -> tuple[bool, int]:
    """Whether the pushed pack still fits, optionally with one memory changed.

    Trimming an over-budget pack down to titles is not the same as keeping it
    inside the budget, because what gets trimmed is exactly the standing rules
    the session was going to be told. A pack that has to be trimmed has already
    failed; the place to catch that is where the change that would cause it is
    being approved, while there is still someone to hand it back to.

    scope_id names the scope riding along with the startup pack, which together
    are what a session working in it receives.

    Passing None measures the heaviest opening, not the lightest. A session
    that never narrows to a scope gets the startup pack alone, but that is not
    the case the ceiling has to survive, and measuring it was how a promotion
    into the startup pack could be accepted and still cost every scoped session
    the current state it opens with: inside the budget for a session that
    learned nothing, over it for every session that learned where it was.
    """
    cur.execute(_SCOPE_INDEX_SQL)
    index = [
        {"name": row["name"], "summary": _one_line(row["description"])} for row in cur.fetchall()
    ]
    cur.execute(_PUSHED_SQL, {"scopes": None, "delivery": str(Delivery.STARTUP_REQUIRED)})
    base = [dict(row) for row in cur.fetchall()]

    if scope_id is not None:
        cur.execute(_PUSHED_SQL, {"scopes": [scope_id], "delivery": str(Delivery.SCOPE_REQUIRED)})
        openings = [base + [dict(row) for row in cur.fetchall()]]
    else:
        cur.execute(_PUSHED_SQL, {"scopes": None, "delivery": str(Delivery.SCOPE_REQUIRED)})
        by_scope: dict[UUID, list[dict[str, Any]]] = {}
        for row in cur.fetchall():
            by_scope.setdefault(row["scope_id"], []).append(dict(row))
        # No scope pushes anything yet, so the startup pack is the whole opening.
        openings = [base + rows for rows in by_scope.values()] or [base]

    cost = max(_total_tokens(index, _with_change(pack, memory_id, content)) for pack in openings)
    return cost <= budget, cost
