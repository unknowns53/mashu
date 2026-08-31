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

from mashu import config, events, match, nominations, redact, tasks, traces
from mashu.capacity import LOCK_NAMESPACE, LOCK_PAIN
from mashu.errors import MashuError, RefusedError

REPORTABLE_KINDS = ("incident", "friction")

#: What shape the answer to a pain takes. A 'rule' is a sentence somebody has
#: to be holding at the moment it applies, and it asks for a memory seat. A
#: 'work' is a change made once, after which nothing has to be remembered —
#: so it asks for nothing here, and belongs on a task instead (v3 9).
#:
#: The reporter chooses, and choosing 'work' is not a way of dodging review:
#: the ledger row is written either way, and it is the ledger that counts what
#: forgetting cost.
PREVENTION_KINDS = ("rule", "work")

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
    prevention_kind: str = "rule",
    task_id: UUID | None = None,
) -> dict[str, Any]:
    """Record one pain, show what it resembles, and nominate when it is proven.

    `prevention` is the matching key throughout: what would have had to be
    known. Two reports of the same hole converge on that sentence long before
    they agree on what went wrong downstream of it.

    `prevention_kind` says which shape that answer has. 'rule' is the v2 path
    and the default, and it can end in a nomination. 'work' never nominates,
    because there is no seat to ask for: what would have stopped the pain is a
    change to make once, and the review desk has no verb for that. Where the
    change goes instead is `task_id`, whose next actions it joins — and when
    no task is named it goes nowhere, which the caller is told rather than
    left to discover from an empty review queue.
    """
    if kind not in REPORTABLE_KINDS:
        raise MashuError(
            f"kind must be one of {', '.join(REPORTABLE_KINDS)}; 'explicit' and 'claimed' are "
            "reserved for a person's own statement, written by mashu remember and by "
            "memory_nominate respectively"
        )
    if prevention_kind not in PREVENTION_KINDS:
        raise MashuError(f"prevention_kind must be one of {', '.join(PREVENTION_KINDS)}")
    if task_id is not None and prevention_kind != "work":
        raise MashuError(
            "a task is where work is filed; a prevention that is a rule is filed by review, "
            "so pass prevention_kind='work' or leave the task out"
        )
    verdict = redact.check(what, prevention)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    # Before the insert and before any matching. Two reports of one hole
    # arriving together would each match only what the other had not yet
    # written, and both would nominate: one rule, two rows in a queue whose
    # whole premise is that it holds a few items a week.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_PAIN))

    # Filing happens before the insert, because the ledger is append-only:
    # where the work went is written into the row or it is not written at all.
    # This is also the one place both advisory locks are held, and the order
    # is pain then project state — nothing takes them the other way round.
    filed_task, filing_note = (None, None)
    if prevention_kind == "work":
        filed_task, filing_note = _file_work(
            cur, prevention=prevention, task_id=task_id, actor=actor
        )

    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, source, created_by,
                            prevention_kind, filed_task)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (kind, what, prevention, scope_id, source, actor, prevention_kind, filed_task),
    )
    entry = cur.fetchone()
    ledger_id = entry["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": kind})

    matches = {
        "ledger": match.similar_ledger(cur, prevention, exclude=ledger_id),
        "traces": match.similar_traces(cur, prevention),
        "tombstones": match.similar_tombstones(cur, prevention),
        "memories": match.similar_active_memories(cur, prevention),
    }
    result: dict[str, Any] = {
        "ledger_id": ledger_id,
        "unchecked": verdict.unchecked,
        "matches": matches,
        "nomination": None,
        "nomination_existing": False,
        "tombstone_suppressed": False,
        "delivery_suspect": False,
        "prevention_kind": prevention_kind,
        "filed_task": filed_task,
    }
    if verdict.malformed:
        result["malformed"] = verdict.malformed

    # Work leaves before the three branches below, all of which decide
    # something about a nomination. What it resembles is still returned, since
    # a fix worth making twice is worth seeing the first one, but none of the
    # branches has anything to say about a row that is not asking for a seat.
    if prevention_kind == "work":
        result["note"] = filing_note
        if filed_task is not None:
            events.record(
                cur,
                "prevention_filed_as_work",
                actor,
                ledger_id=ledger_id,
                detail={"task_id": str(filed_task)},
            )
        return result

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

    # A pain that lands on a rule already being delivered is not the standard
    # in section 3 being met once more; it is the specification's own
    # falsification criterion firing (12). The rule was admitted, it is being
    # pushed, and it still did not reach the moment it was needed — so the
    # thing to fix is the delivery, and a second seat for the same sentence
    # would fix nothing while looking like a fix. The ledger row stays, since
    # that is the evidence the delivery is failing.
    delivered = matches["memories"][0] if matches["memories"] else None
    if delivered and delivered["score"] >= threshold:
        result["delivery_suspect"] = True
        result["note"] = (
            "this rule is already active and being delivered as "
            f"'{delivered['delivery']}'. No nomination was created: a third "
            "occurrence indicts the delivery, not the entrance standard. Ask "
            "whether it should move to a guard on the act where it is needed, "
            "or whether the wording is not recognisable at the moment it "
            "applies."
        )
        events.record(
            cur,
            "delivery_failure_suspected",
            actor,
            memory_id=delivered["memory_id"],
            ledger_id=ledger_id,
            detail={"delivery": delivered["delivery"], "score": float(delivered["score"])},
        )
        return result

    # A candidate already waiting for this rule is not a second candidate.
    # Two rows in the queue saying the same thing cost a person two decisions
    # and admit one rule, and the duplicate is invisible until it is read. The
    # new pain goes underneath the waiting one instead, because how many times
    # this has now happened is most of what the reviewer is weighing.
    waiting = match.similar_pending_nominations(cur, prevention, limit=1)
    if waiting and waiting[0]["score"] >= threshold:
        existing = nominations.add_evidence(
            cur, waiting[0]["nomination_id"], ledger_id, actor=actor
        )
        if existing is not None:
            result["nomination"] = existing
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


def _file_work(
    cur: psycopg.Cursor, *, prevention: str, task_id: UUID | None, actor: str
) -> tuple[UUID | None, str]:
    """Put the change on the task that will make it, and say which happened.

    Returns where it landed and the sentence the caller shows. A refusal from
    the task side — closed, five next actions already, no room in the project
    budget — must not take the pain down with it: the pain happened, and that
    is the part a later reader cannot reconstruct. So the append runs inside a
    savepoint and a refusal comes back as words, with the ledger row still to
    be written behind it.

    What is caught is a refusal, not a fault. A psycopg error from underneath
    — a constraint nothing checked first, a trigger — is left to propagate and
    take the transaction with it, because a store that answered a broken write
    with 'the fix still needs a home' would be reporting the wrong thing in
    the one case worth interrupting for.
    """
    unseated = (
        "recorded as work, so no nomination was created: a change made once is not a rule "
        "to be admitted. "
    )
    if task_id is None:
        return None, unseated + (
            "It was filed nowhere, because no task was named — put it on one "
            "(task_update / task_checkpoint) or it lives only in the ledger."
        )
    try:
        with cur.connection.transaction():
            appended = tasks.append_next_action(cur, task_id, prevention, actor=actor)
    except MashuError as error:
        return None, unseated + (
            f"Filing it on the task failed: {error}. The ledger row stands; the fix still "
            "needs a home."
        )
    return task_id, unseated + (
        "It is on the task's next actions."
        if appended["appended"]
        else "The task's next actions already carried it."
    )


def _best_prior(
    cur: psycopg.Cursor, matches: dict[str, list[dict[str, Any]]], threshold: float, *, actor: str
) -> UUID | None:
    """The ledger row a second friction can point back at, if there is one.

    Only a friction or a trace qualifies (4.1). A friction reported as work
    qualifies too: what it recorded is that somebody worked the same thing out
    a second time, which is what a re-derivation is, and the reporter's view
    that the answer was a code change is a view about the answer rather than
    about whether the hole is real. It nominates nothing itself; it can be the
    prior half of somebody else's.

    An incident finished its own nomination when it was reported, and
    'explicit' and 'claimed' rows are
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
