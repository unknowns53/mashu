"""Record reported pains and connect them to matching evidence."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, match, memories, nominations, redact, tasks, topics
from mashu.capacity import LOCK_NAMESPACE, LOCK_PAIN
from mashu.errors import MashuError

#: `friction` rows stay in the ledger as history, but a pain is reported as an incident.
REPORTABLE_KINDS = ("incident",)

#: What shape the answer to a pain takes.
PREVENTION_KINDS = ("rule", "work")

#: The kinds a person's own statement takes.
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
    session: UUID | None = None,
    violated_memory_id: UUID | None = None,
) -> dict[str, Any]:
    """Record one pain, show what it resembles, and nominate when it is proven.

    `session` names the MCP server process reporting it, so a pain against a topic rule
    can say whether that session had read the topic. `violated_memory_id` names an active
    rule the reporter says was delivered and not followed, which similarity cannot find
    when the pain is worded differently from the rule.
    """
    if kind == "friction":
        raise MashuError(
            "kind 'friction' is no longer reported: a repeated lookup is not a pain until it "
            "causes wrong work, so report that as kind 'incident'"
        )
    if kind not in REPORTABLE_KINDS:
        raise MashuError(
            f"kind must be {' or '.join(REPORTABLE_KINDS)}; 'explicit' and 'claimed' are "
            "reserved for a person's own statement, written by mashu remember and by "
            "memory_nominate or memory_admit respectively"
        )
    if prevention_kind not in PREVENTION_KINDS:
        raise MashuError(f"prevention_kind must be one of {', '.join(PREVENTION_KINDS)}")
    if task_id is not None and prevention_kind != "work":
        raise MashuError(
            "a task is where work is filed; a prevention that is a rule is filed by review, "
            "so pass prevention_kind='work' or leave the task out"
        )
    if prevention_kind == "rule":
        # A rule's prevention becomes a candidate's body, and the pain's own account is `what`.
        memories.check_body(prevention, field="prevention", rest="what")
    verdict = redact.gate({"what": what, "prevention": prevention})
    violated = _violated(cur, violated_memory_id) if violated_memory_id is not None else None

    # Before the insert and before any matching.
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, LOCK_PAIN))

    # Store the task filing on the append-only ledger row.
    filed_task, filing_note = (None, None)
    if prevention_kind == "work":
        filed_task, filing_note = _file_work(
            cur, prevention=prevention, task_id=task_id, actor=actor
        )

    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, source, created_by,
                            prevention_kind, filed_task, violated_memory_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            kind,
            what,
            prevention,
            scope_id,
            source,
            actor,
            prevention_kind,
            filed_task,
            violated_memory_id,
        ),
    )
    entry = cur.fetchone()
    ledger_id = entry["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": kind})

    matches = {
        "ledger": match.similar_ledger(cur, prevention, exclude=ledger_id),
        "tombstones": match.similar_tombstones(cur, prevention),
        "memories": match.similar_active_memories(cur, prevention),
    }
    retirement_conflicts = [
        row
        for row in match.similar_tombstones(cur, prevention, limit=None)
        if row["score"] >= config.match_threshold()
    ]
    conflict_ids = [row["memory_id"] for row in retirement_conflicts]
    result: dict[str, Any] = {
        "ledger_id": ledger_id,
        "unchecked": verdict.unchecked,
        "matches": matches,
        "nomination": None,
        "nomination_existing": False,
        "tombstone_suppressed": False,
        "retirement_conflicts": retirement_conflicts,
        "delivery_suspect": False,
        "prevention_kind": prevention_kind,
        "filed_task": filed_task,
        "violated_memory_id": violated_memory_id,
    }
    if verdict.malformed:
        result["malformed"] = verdict.malformed

    # The reporter's word names the rule whatever the prevention is, so this comes first.
    if violated is not None:
        result["delivery_suspect"] = True
        _suspect_delivery(
            cur, violated, ledger_id=ledger_id, actor=actor, session=session, found_by="reported"
        )

    # Work leaves before the branches below, all of which decide something about a nomination.
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

    # Record a delivery failure when an active rule did not prevent the pain.
    delivered = matches["memories"][0] if matches["memories"] else None
    if violated is None and delivered and delivered["score"] >= threshold:
        violated = delivered
        result["delivery_suspect"] = True
        _suspect_delivery(
            cur,
            delivered,
            ledger_id=ledger_id,
            actor=actor,
            session=session,
            found_by="similarity",
            score=float(delivered["score"]),
        )
    if violated is not None:
        result["note"] = (
            "this rule is already active and being delivered as "
            f"'{violated['delivery']}'. No nomination was created: a pain under an active "
            "rule indicts the delivery, not the entrance standard. Ask "
            "whether it should move to a topic read when the work it governs "
            "begins, or whether the wording is not recognisable at the moment it "
            "applies."
        )
        if (
            violated["delivery"] == "topic"
            and _topic_read(cur, violated["topic_id"], session) is False
        ):
            result["note"] += (
                " This session never read the topic, so first ask whether its trigger "
                "names the work in words recognisable when it begins."
            )
        return result

    # A candidate already waiting for this rule is not a second candidate.
    waiting = match.similar_pending_nominations(cur, prevention, limit=1)
    if waiting and waiting[0]["score"] >= threshold:
        existing = nominations.add_evidence(
            cur, waiting[0]["nomination_id"], ledger_id, actor=actor, conflicts=conflict_ids
        )
        if existing is not None:
            existing = (
                nominations.refresh_conflicts(
                    cur, existing["nomination_id"], conflict_ids, actor=actor
                )
                or existing
            )
            result["nomination"] = existing
            result["nomination_existing"] = True
            return result

    result["nomination"] = nominations.create_nomination(
        cur,
        content=prevention,
        kind="incident",
        evidence=[ledger_id],
        actor=actor,
        scope_id=scope_id,
        conflicts=conflict_ids,
    )
    return result


def _violated(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any]:
    """The rule a reporter names as not followed, refused unless it is in force."""
    cur.execute("SELECT * FROM memory WHERE memory_id = %s", (memory_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no memory {memory_id}")
    if row["status"] != "active":
        when = row["retired_at"].date().isoformat() if row["retired_at"] else "earlier"
        raise MashuError(
            f"memory {memory_id} was retired ({when}) and is not delivered, so it cannot have "
            "been the rule not followed; leave violated_memory_id out"
        )
    return row


def _suspect_delivery(
    cur: psycopg.Cursor,
    rule: dict[str, Any],
    *,
    ledger_id: UUID,
    actor: str,
    session: UUID | None,
    found_by: str,
    score: float | None = None,
) -> None:
    """Record that an active rule did not prevent this pain, and how the rule was found."""
    detail: dict[str, Any] = {"delivery": rule["delivery"], "found_by": found_by}
    if score is not None:
        detail["score"] = score
    if rule["delivery"] == "topic":
        detail.update(
            topic_id=str(rule["topic_id"]),
            topic_read=_topic_read(cur, rule["topic_id"], session),
            topic_action=_topic_action(cur, rule["topic_id"]),
        )
    events.record(
        cur,
        "delivery_failure_suspected",
        actor,
        memory_id=rule["memory_id"],
        ledger_id=ledger_id,
        detail=detail,
    )


def _file_work(
    cur: psycopg.Cursor, *, prevention: str, task_id: UUID | None, actor: str
) -> tuple[UUID | None, str]:
    """Put the change on the task that will make it, and say which happened."""
    unseated = (
        "recorded as work, so no nomination was created: a change made once is not a rule "
        "to be admitted. "
    )
    if task_id is None:
        return None, unseated + (
            "It was filed nowhere, because no task was named — put it on one "
            "(task_checkpoint) or it lives only in the ledger."
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


def _topic_action(cur: psycopg.Cursor, topic_id: UUID) -> str | None:
    """The action a topic stands before, if the store's topics carry one yet."""
    if not topics.has_actions(cur):
        return None
    cur.execute("SELECT action FROM topic WHERE topic_id = %s", (topic_id,))
    return cur.fetchone()["action"]


def _topic_read(cur: psycopg.Cursor, topic_id: UUID, session: UUID | None) -> bool | None:
    """Whether this session read the topic; unknown when the pain came from outside MCP."""
    if session is None:
        return None
    cur.execute(
        """
        SELECT 1 FROM event_log
        WHERE event_type = 'topic_read'
          AND detail->>'topic_id' = %s AND detail->>'session' = %s
        LIMIT 1
        """,
        (str(topic_id), str(session)),
    )
    return cur.fetchone() is not None
