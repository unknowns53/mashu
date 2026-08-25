"""The pain ledger (specification 4.1).

Not knowledge. A ledger row is never delivered to anyone and never asserts
anything to another session. It does two jobs: it records the first time
something hurt, cheaply enough that recording it is not itself a cost, and it
gives the second time something to collide with.

The price of the standard in section 3 is that the first pain is paid in full.
What the ledger buys back is that it is paid exactly once — the second
occurrence arrives already proven, instead of being another isolated
complaint.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, match, nominations, redact, traces
from mashu.capacity import LOCK_NAMESPACE, LOCK_PAIN
from mashu.errors import MashuError, RefusedError

REPORTABLE_KINDS = ("incident", "friction")

#: The kinds a person's own statement takes. Neither is a pain, so neither is
#: reportable here and neither counts as the first half of a rederivation.
STATED_KINDS = ("explicit", "claimed")


def report_pain(
    cur: psycopg.Cursor,
    *,
    kind: str,
    what: str,
    prevention: str,
    actor: str,
    scope_id: UUID | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Record one pain, show what it resembles, and nominate when it is proven.

    `prevention` is the matching key throughout: what would have had to be
    known. Two reports of the same hole converge on that sentence long before
    they agree on what went wrong downstream of it.
    """
    if kind not in REPORTABLE_KINDS:
        raise MashuError(
            f"kind must be one of {', '.join(REPORTABLE_KINDS)}; 'explicit' and 'claimed' are "
            "reserved for a person's own statement, written by mashu remember and by "
            "memory_nominate respectively"
        )
    verdict = redact.check(what, prevention)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    # Before the insert and before any matching. Two reports of one hole
    # arriving together would each match only what the other had not yet
    # written, and both would nominate: one rule, two rows in a queue whose
    # whole premise is that it holds a few items a week.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_PAIN))

    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, source, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (kind, what, prevention, scope_id, source, actor),
    )
    entry = cur.fetchone()
    ledger_id = entry["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": kind})

    matches = {
        "ledger": match.similar_ledger(cur, prevention, exclude=ledger_id),
        "traces": match.similar_traces(cur, prevention),
        "tombstones": match.similar_tombstones(cur, prevention),
    }
    result: dict[str, Any] = {
        "ledger_id": ledger_id,
        "unchecked": verdict.unchecked,
        "matches": matches,
        "nomination": None,
        "nomination_existing": False,
        "tombstone_suppressed": False,
    }
    if verdict.malformed:
        result["malformed"] = verdict.malformed

    threshold = config.match_threshold()

    # A pain that lands on retired knowledge does not nominate. The automatic
    # path re-submitting refuted content would resurrect it with the reviewer
    # never shown the refutation; what comes back instead is the retire
    # reason, and overriding a retirement stays a human act (mashu remember),
    # made with that reason in view.
    best_tombstone = matches["tombstones"][0] if matches["tombstones"] else None
    if best_tombstone and best_tombstone["score"] >= threshold:
        result["tombstone_suppressed"] = True
        result["note"] = (
            "a retired memory already covers this; its retire reason is the answer. "
            "No nomination was created. If the retirement itself is wrong, that is a "
            "human decision to make with the reason in view (mashu remember)."
        )
        return result

    # A candidate already waiting for this rule is not a second candidate.
    # Two rows in the queue saying the same thing cost a person two decisions
    # and admit one rule, and the duplicate is invisible until it is read.
    waiting = match.similar_pending_nominations(cur, prevention, limit=1)
    if waiting and waiting[0]["score"] >= threshold:
        result["nomination"] = waiting[0]
        result["nomination_existing"] = True
        return result

    if kind == "incident":
        result["nomination"] = nominations.create_nomination(
            cur,
            content=prevention,
            kind="incident",
            evidence=[ledger_id],
            actor=actor,
            scope_id=scope_id,
        )
        return result

    prior = _best_prior(cur, matches, threshold, actor=actor)
    if prior is not None:
        result["nomination"] = nominations.create_nomination(
            cur,
            content=prevention,
            kind="rederivation",
            evidence=[prior, ledger_id],
            actor=actor,
            scope_id=scope_id,
        )
    return result


def _best_prior(
    cur: psycopg.Cursor, matches: dict[str, list[dict[str, Any]]], threshold: float, *, actor: str
) -> UUID | None:
    """The ledger row a second friction can point back at, if there is one.

    Only a friction or a trace qualifies (4.1). An incident finished its own
    nomination when it was reported, and 'explicit' and 'claimed' rows are
    somebody stating a rule, not anybody having worked something out twice.
    Counting those would let a single look-up next to an existing statement
    call itself a re-derivation, which is the standard in section 3 being met
    by paraphrase.

    What is filtered is the choice, not the display: the caller still shows
    every match, because a reporter deciding whether this is the same hole
    wants to see the statement too.

    Ledger rows and traces compete on the same scale here. A trace that wins
    is frozen on the spot, because the candidate it supports will outlive the
    thirty days the trace has left.
    """
    frictions = [row for row in matches["ledger"] if row["kind"] == "friction"]
    best_ledger = frictions[0] if frictions else None
    best_trace = matches["traces"][0] if matches["traces"] else None

    candidates = [c for c in (best_ledger, best_trace) if c and c["score"] >= threshold]
    if not candidates:
        return None
    best = max(candidates, key=lambda row: row["score"])
    if "trace_id" in best:
        return traces.freeze_trace(cur, best, actor=actor)["ledger_id"]
    return best["ledger_id"]


def ledger_entries(
    cur: psycopg.Cursor, *, limit: int = 20, scope_id: UUID | None = None
) -> list[dict[str, Any]]:
    """The recent record, newest first."""
    cur.execute(
        """
        SELECT l.*, s.name AS scope_name
        FROM ledger l LEFT JOIN scope s ON s.scope_id = l.scope_id
        WHERE (%(scope)s::uuid IS NULL OR l.scope_id = %(scope)s::uuid)
        ORDER BY l.created_at DESC, l.ledger_id
        LIMIT %(limit)s
        """,
        {"scope": scope_id, "limit": limit},
    )
    return cur.fetchall()
