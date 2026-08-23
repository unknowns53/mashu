"""Proposals and the commit gate applied to them (specifications 15, 17, 18.1).

Every change to the knowledge state enters here. What differs between the three
commit lines is not whether the change is written but whether it becomes
current:

- auto commit: written and adopted at once
- candidate commit: written, left unadopted, and queued for review. The version
  exists so that layer 2 of retrieval can hand its content to agents while the
  review is outstanding (21.1); approving it moves the active pointer
- human review required: nothing is written, except that a provisional entity
  is still created so the agent is not blocked (20.1)

The proposal remembers the version it wrote in applied_version, which is what
an approval later adopts.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from mashu import events, resolution, store
from mashu.errors import DuplicateProposalError, NotFoundError, ProposalError
from mashu.gate import CommitDecision, GateRuling, classify
from mashu.models import (
    EntityStatus,
    EventType,
    MemoryType,
    ProposalOperation,
    ProposalStatus,
    SourceType,
    VersionStatus,
)

_DECIDED = frozenset(
    {ProposalStatus.APPROVED, ProposalStatus.REJECTED, ProposalStatus.AUTO_COMMITTED}
)


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def get(cur: psycopg.Cursor, proposal_id: UUID) -> dict:
    """One proposal row."""
    cur.execute("SELECT * FROM proposal WHERE proposal_id = %s", (proposal_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no proposal {proposal_id}")
    return row


def find_duplicate_proposals(
    cur: psycopg.Cursor,
    *,
    target_memory: UUID | None,
    scope_id: UUID | None = None,
    title: str | None = None,
) -> list[dict]:
    """Proposals that already covered this change (specification 15.1).

    Two kinds count. One is still waiting, which is the case section 15.1 is
    written for: proposing it again grows the queue with duplicates of one
    unreviewed idea. The other was turned down, which section 15.1 does not
    mention but which matters more, because a rejection the proposer never
    learns about is a rejection it will walk into again.

    A turned-down proposal keeps its decision_reason (specification 15), so the
    row carries the reviewer's words back to the proposer. Nothing about the
    version's own status is consulted here; whether the same idea has already
    been ruled on is a fact about the procedure, and the procedure is what the
    proposal table records.

    For a change to an existing entity the test is exact: same target. For a
    creation there is no target yet, so the test is on the title within the
    scope. Entity resolution (20) is what catches a differently worded rival;
    this check catches the proposer repeating itself.
    """
    if target_memory is not None:
        cur.execute(
            """
            SELECT proposal_id, actor, operation, payload, status, decision_reason,
                   created_at, decided_at,
                   EXTRACT(DAY FROM now() - created_at)::int AS days_pending
            FROM proposal
            WHERE status IN ('pending', 'rejected') AND target_memory = %s
            ORDER BY created_at
            """,
            (target_memory,),
        )
        return cur.fetchall()

    if scope_id is None or title is None:
        return []
    cur.execute(
        """
        SELECT proposal_id, actor, operation, payload, status, decision_reason,
               created_at, decided_at,
               EXTRACT(DAY FROM now() - created_at)::int AS days_pending
        FROM proposal
        WHERE status IN ('pending', 'rejected')
          AND operation = 'create'
          AND payload ->> 'scope_id' = %s
          AND payload ->> 'title' = %s
        ORDER BY created_at
        """,
        (str(scope_id), title),
    )
    return cur.fetchall()


def session_queue(cur: psycopg.Cursor) -> list[dict]:
    """Pending proposals grouped into session bundles (specification 18.1).

    Within a bundle the order is observation, then interpretation, then
    decision: grounds before conclusions, so the context is built once.

    Section 18.1 names only those three, and putting the other five at the end
    would leave a bundle reading decision first and its supporting fact after,
    which is the arrangement the ordering exists to prevent. So every type sits
    on the same axis: what was seen, what it was taken to mean, what was
    settled.

    Proposals with no session sit in one unnamed bundle rather than being
    dropped, so nothing can go missing from the queue by lacking provenance.

    Within a rank the tiebreak is seq and not created_at. created_at is now(),
    which is transaction start time, so a batch of proposals written in one
    transaction carries a single identical timestamp and leaves the order
    inside a rank to whatever the scan happens to return. The import of 27.1 is
    exactly such a batch.
    """
    cur.execute(
        """
        SELECT p.proposal_id, p.session_id, p.actor, p.operation, p.target_memory,
               p.payload, p.created_at, p.seq,
               EXTRACT(DAY FROM now() - p.created_at)::int AS days_pending,
               COALESCE(e.type, p.payload ->> 'type') AS memory_type,
               COALESCE(e.title, p.payload ->> 'title') AS title,
               s.name AS scope_name
        FROM proposal p
        LEFT JOIN memory_entity e ON e.memory_id = p.target_memory
        LEFT JOIN scope s
               ON s.scope_id = COALESCE(e.scope_id, (p.payload ->> 'scope_id')::uuid)
        WHERE p.status = 'pending'
        ORDER BY p.session_id NULLS LAST, p.seq
        """
    )
    rows = cur.fetchall()

    rank = {
        # what was seen
        str(MemoryType.OBSERVATION): 0,
        str(MemoryType.FACT): 0,
        str(MemoryType.STATE): 0,
        # what it was taken to mean
        str(MemoryType.INTERPRETATION): 1,
        str(MemoryType.HYPOTHESIS): 1,
        # what was settled, and what follows from it
        str(MemoryType.DECISION): 2,
        str(MemoryType.TASK): 2,
        str(MemoryType.PREFERENCE): 2,
    }
    bundles: dict[UUID | None, list[dict]] = {}
    for row in rows:
        bundles.setdefault(row["session_id"], []).append(row)

    out = []
    for session_id, items in bundles.items():
        items.sort(key=lambda r: (rank.get(r["memory_type"], 3), r["seq"]))
        out.append(
            {
                "session_id": session_id,
                "count": len(items),
                "days_pending": max(i["days_pending"] for i in items),
                "proposals": items,
            }
        )
    out.sort(key=lambda b: -b["days_pending"])
    return out


_ORPHAN_SQL = """
SELECT e.memory_id, e.title, e.type, v.version_id, v.created_by, v.created_at,
       EXTRACT(DAY FROM now() - v.created_at)::int AS days_pending
FROM memory_version v
JOIN memory_entity e ON e.memory_id = v.memory_id
LEFT JOIN proposal p
       ON p.applied_version = v.version_id AND p.status = 'pending'
WHERE v.status = 'candidate'
  AND (e.active_version IS NULL OR e.active_version <> v.version_id)
  AND p.proposal_id IS NULL
ORDER BY v.created_at
"""


def orphaned_candidates(cur: psycopg.Cursor) -> list[dict]:
    """Candidate versions that no pending proposal is waiting on.

    Layer 2 of retrieval is built from versions and the review queue is built
    from proposals, so a candidate written straight into the store rather than
    proposed is in a state with no way out: agents can read it, tagged
    unreviewed, and no review will ever settle it.

    Normal operation cannot produce one, because everything enters through
    propose. Bulk work can: the migration of 27.1 imports existing content as
    candidates, and importing it by writing versions directly would strand
    every one of them. Surfacing them is cheaper than trusting that nobody
    does that.
    """
    cur.execute(_ORPHAN_SQL)
    return cur.fetchall()


# --------------------------------------------------------------------------
# writing a proposal
# --------------------------------------------------------------------------
def propose(
    cur: psycopg.Cursor,
    *,
    actor: str,
    operation: ProposalOperation,
    payload: dict[str, Any],
    target_memory: UUID | None = None,
    based_on_version: UUID | None = None,
    session_id: UUID | None = None,
    allow_duplicate: bool = False,
    allow_similar: bool = False,
    hold_for_review: str | None = None,
) -> dict[str, Any]:
    """Record a proposed change and take it as far as the gate allows.

    Returns the proposal row together with the gate's ruling, so the caller can
    tell an applied change from one that is now waiting.

    hold_for_review holds the change as a candidate and records the given
    reason. Section 27.1 needs it: the content being imported from the native
    memory files is summary prose with observation and interpretation fused
    together, and letting the auto line apply any of it would import the
    contamination Mashu exists to keep out. It can only ever make the gate
    stricter, and it never overrides a human review requirement.

    Three checks run before the gate does, and all of them stop by raising
    rather than by writing. The first is a vocabulary check: rejected is the
    reviewer's verdict on a proposal, so a proposal asking for it would be a
    proposal asking to be turned down.

    The other two are not the same check. The duplicate check (15.1) looks
    for a proposal that has already been made and has already been ruled on or
    is still waiting; entity resolution (20) looks for a concept that already
    exists.
    Passing allow_duplicate or allow_similar says the caller has looked at what
    was found and judged this different, which is the choice section 20 gives
    the agent. Neither flag waives the other, and no flag waives the first
    check: a status the vocabulary does not offer the proposer is not a
    judgement call.
    """
    operation = ProposalOperation(operation)

    if operation is ProposalOperation.CHANGE_STATUS and payload.get("status") == str(
        VersionStatus.REJECTED
    ):
        raise ProposalError(
            "rejected is not a status anything can propose; it is what review "
            "does to a proposal. To retire the content, propose disproven or "
            "dormant (specification 11)"
        )

    if not allow_duplicate:
        existing = find_duplicate_proposals(
            cur,
            target_memory=target_memory,
            scope_id=payload.get("scope_id"),
            title=payload.get("title"),
        )
        if existing:
            turned_down = [row for row in existing if row["status"] == "rejected"]
            if turned_down:
                why = turned_down[-1]["decision_reason"]
                detail = f"{len(turned_down)} of them was turned down: {why}"
            else:
                detail = f"{len(existing)} of them is still waiting for review"
            raise DuplicateProposalError(
                f"this change has already been proposed; {detail}. Look at it, "
                f"and pass allow_duplicate to propose anyway",
                existing,
            )

    if operation is ProposalOperation.CREATE and "entity_status" not in payload:
        similar = resolution.needs_similar_review(
            cur, scope_id=payload["scope_id"], title=payload["title"]
        )
        if similar and not allow_similar:
            raise resolution.SimilarEntityError(
                f"{len(similar)} entity(ies) in this scope already look like "
                f"'{payload['title']}'; add a version to one, or pass "
                f"allow_similar to create a provisional entity alongside them",
                similar,
            )
        if similar:
            # Section 20: creating despite the similarity is the agent's to
            # choose, and the resulting entity waits for a human either way.
            payload = dict(payload, entity_status=str(EntityStatus.PROVISIONAL))

    ruling = _rule(cur, operation, payload, target_memory)
    if hold_for_review and ruling.decision is not CommitDecision.HUMAN_REVIEW:
        # The hold is a fact about where the change came from, so it is
        # recorded whether or not the gate had already stopped it. Dropping it
        # once the gate agrees would leave the reviewer of an imported item
        # reading a reason that says nothing about the import.
        reason = (
            hold_for_review
            if ruling.decision is CommitDecision.AUTO
            else f"{ruling.reason}; {hold_for_review}"
        )
        ruling = GateRuling(CommitDecision.CANDIDATE, reason)
    status = (
        ProposalStatus.AUTO_COMMITTED
        if ruling.decision is CommitDecision.AUTO
        else ProposalStatus.PENDING
    )

    cur.execute(
        """
        INSERT INTO proposal (actor, operation, target_memory, based_on_version,
                              session_id, payload, status, reviewer, decided_at,
                              decision_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s = 'pending' THEN NULL ELSE now() END, %s)
        RETURNING *
        """,
        (
            actor,
            str(operation),
            target_memory,
            based_on_version,
            session_id,
            Jsonb(payload),
            str(status),
            None,
            str(status),
            ruling.reason,
        ),
    )
    proposal = cur.fetchone()
    events.record(
        cur,
        EventType.PROPOSAL_CREATED,
        actor,
        proposal_id=proposal["proposal_id"],
        memory_id=target_memory,
        detail={"operation": str(operation), "gate": str(ruling.decision), "why": ruling.reason},
    )

    applied_version = _write_now(
        cur, proposal=proposal, ruling=ruling, payload=payload, actor=actor
    )
    if applied_version is not None:
        cur.execute(
            "UPDATE proposal SET applied_version = %s WHERE proposal_id = %s RETURNING *",
            (applied_version, proposal["proposal_id"]),
        )
        proposal = cur.fetchone()

    if ruling.decision is CommitDecision.AUTO:
        events.record(
            cur,
            EventType.PROPOSAL_COMMITTED,
            actor,
            proposal_id=proposal["proposal_id"],
            memory_id=proposal["target_memory"],
            version_id=applied_version,
            detail={"auto": True, "why": ruling.reason},
        )

    return {"proposal": proposal, "ruling": ruling}


# --------------------------------------------------------------------------
# deciding
# --------------------------------------------------------------------------
def approve(
    cur: psycopg.Cursor,
    proposal_id: UUID,
    *,
    reviewer: str,
    reason: str | None = None,
) -> dict:
    """Accept a pending proposal and make its change current."""
    proposal = _pending(cur, proposal_id)
    _apply_on_approval(cur, proposal, actor=reviewer)

    cur.execute(
        """
        UPDATE proposal
        SET status = 'approved', reviewer = %s, decided_at = now(), decision_reason = %s
        WHERE proposal_id = %s
        RETURNING *
        """,
        (reviewer, reason, proposal_id),
    )
    decided = cur.fetchone()
    events.record(
        cur,
        EventType.PROPOSAL_COMMITTED,
        reviewer,
        proposal_id=proposal_id,
        memory_id=decided["target_memory"],
        version_id=decided["applied_version"],
        detail={"auto": False, "reason": reason},
    )
    return decided


def reject(cur: psycopg.Cursor, proposal_id: UUID, *, reviewer: str, reason: str) -> dict:
    """Turn a pending proposal down. The reason is required.

    A candidate that was written at propose time is not deleted but moved to
    rejected, so that layer 3 of retrieval can tell the agent this was looked
    at and turned down. Deleting it would let the same proposal come back
    unremarked.

    rejected rather than dormant, though dormant would also have kept the row.
    dormant says the content is not useful now but may be worth revisiting, and
    that is a reading of the knowledge; being turned down is a fact about the
    proposal and says nothing either way about the content. Folding one into
    the other would leave any later sweep for re-evaluation candidates picking
    through the leftovers of every rejection.

    The reviewer's words go onto the version as well as onto the proposal.
    version.reason means the reason the version is in the state it is now
    (specification 26), and the reason it is rejected is what the reviewer
    wrote, so layer 3 can hand it over without reaching into the procedure.
    """
    if not reason:
        raise ProposalError("a rejection has to record why (specification 31)")
    proposal = _pending(cur, proposal_id)

    if proposal["applied_version"] is not None:
        version = store.get_version(cur, proposal["applied_version"])
        if VersionStatus(version["status"]) is VersionStatus.CANDIDATE:
            store.set_status(
                cur,
                version_id=proposal["applied_version"],
                target=VersionStatus.REJECTED,
                actor=reviewer,
                reason=reason,
            )

    cur.execute(
        """
        UPDATE proposal
        SET status = 'rejected', reviewer = %s, decided_at = now(), decision_reason = %s
        WHERE proposal_id = %s
        RETURNING *
        """,
        (reviewer, reason, proposal_id),
    )
    decided = cur.fetchone()
    events.record(
        cur,
        EventType.PROPOSAL_REJECTED,
        reviewer,
        proposal_id=proposal_id,
        memory_id=decided["target_memory"],
        version_id=decided["applied_version"],
        detail={"reason": reason},
    )
    return decided


def approve_bundle(
    cur: psycopg.Cursor,
    session_id: UUID | None,
    *,
    reviewer: str,
    reason: str | None = None,
    skip: set[UUID] | None = None,
) -> list[dict]:
    """Approve a session's pending proposals in dependency order (18.1).

    The order matters on approval as much as on reading: a decision adopted
    before the observation it rests on would briefly be current without its
    grounds.
    """
    skip = skip or set()
    bundle = next((b for b in session_queue(cur) if b["session_id"] == session_id), None)
    if bundle is None:
        return []
    return [
        approve(cur, item["proposal_id"], reviewer=reviewer, reason=reason)
        for item in bundle["proposals"]
        if item["proposal_id"] not in skip
    ]


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------
def _pending(cur: psycopg.Cursor, proposal_id: UUID) -> dict:
    proposal = get(cur, proposal_id)
    if ProposalStatus(proposal["status"]) in _DECIDED:
        raise ProposalError(
            f"proposal {proposal_id} was already {proposal['status']}"
            f" by {proposal['reviewer'] or 'the system'}"
        )
    return proposal


def _rule(
    cur: psycopg.Cursor,
    operation: ProposalOperation,
    payload: dict[str, Any],
    target_memory: UUID | None,
) -> GateRuling:
    """Gather what the gate needs from the payload and the target entity."""
    entity_status = EntityStatus(payload.get("entity_status", EntityStatus.ACTIVE))
    target_status = payload.get("status")
    type_ = payload.get("type")

    if target_memory is not None and type_ is None:
        type_ = store.get_entity(cur, target_memory)["type"]

    return classify(
        operation=operation,
        type=MemoryType(type_) if type_ else None,
        source_type=SourceType(payload["source_type"]) if payload.get("source_type") else None,
        entity_status=entity_status,
        target_status=VersionStatus(target_status) if target_status else None,
        switches_active=bool(payload.get("switches_active")),
    )


def _as_uuid(value: UUID | str) -> UUID:
    """A payload id, which is a string once it has been through JSONB."""
    return value if isinstance(value, UUID) else UUID(str(value))


def _evidence(payload: dict[str, Any]) -> list[UUID] | None:
    """Read the grounds a proposal names, which arrive as strings through JSONB."""
    raw = payload.get("evidence")
    if not raw:
        return None
    return [_as_uuid(item) for item in raw]


def _write_now(
    cur: psycopg.Cursor,
    *,
    proposal: dict,
    ruling: GateRuling,
    payload: dict[str, Any],
    actor: str,
) -> UUID | None:
    """Write whatever this commit line writes at propose time.

    Only create and update_version write here. The rest of the human review
    line changes existing state and so waits for the approval.
    """
    operation = ProposalOperation(proposal["operation"])
    adopt = ruling.decision is CommitDecision.AUTO

    if operation is ProposalOperation.CREATE:
        _, version_id = store.create_entity(
            cur,
            scope_id=payload["scope_id"],
            type=MemoryType(payload["type"]),
            title=payload["title"],
            content=payload["content"],
            directive=payload.get("directive"),
            evidence=_evidence(payload),
            source_type=SourceType(payload["source_type"]),
            source_reference=payload.get("source_reference"),
            created_by=proposal["actor"],
            actor=actor,
            adopt=adopt,
            entity_status=EntityStatus(payload.get("entity_status", EntityStatus.ACTIVE)),
        )
        cur.execute(
            "UPDATE proposal SET target_memory = %s WHERE proposal_id = %s",
            (store.get_version(cur, version_id)["memory_id"], proposal["proposal_id"]),
        )
        return version_id

    if operation is ProposalOperation.CHANGE_STATUS:
        # Section 17 puts a simple task completion on the auto line, and a
        # status change the user states lands there too. Without this the
        # proposal was marked auto_committed and the version did not move,
        # which is section 1's "a finished task is treated as unfinished"
        # produced by the thing built to prevent it.
        if not adopt:
            return None
        store.set_status(
            cur,
            version_id=_as_uuid(payload["version_id"]),
            target=VersionStatus(payload["status"]),
            actor=actor,
            reason=payload["reason"],
        )
        return None

    if operation is ProposalOperation.UPDATE_VERSION:
        if ruling.decision is CommitDecision.HUMAN_REVIEW:
            return None
        return store.add_version(
            cur,
            memory_id=proposal["target_memory"],
            content=payload["content"],
            directive=payload.get("directive"),
            evidence=_evidence(payload),
            source_type=SourceType(payload["source_type"]),
            source_reference=payload.get("source_reference"),
            created_by=proposal["actor"],
            actor=actor,
            based_on_version=proposal["based_on_version"],
            adopt=adopt,
        )

    return None


def _apply_on_approval(cur: psycopg.Cursor, proposal: dict, *, actor: str) -> None:
    """Carry out what approval means for each operation."""
    operation = ProposalOperation(proposal["operation"])
    payload = proposal["payload"]

    if operation in (ProposalOperation.CREATE, ProposalOperation.UPDATE_VERSION):
        version_id = proposal["applied_version"]
        if version_id is None:
            version_id = store.add_version(
                cur,
                memory_id=proposal["target_memory"],
                content=payload["content"],
                directive=payload.get("directive"),
                evidence=_evidence(payload),
                source_type=SourceType(payload["source_type"]),
                source_reference=payload.get("source_reference"),
                created_by=proposal["actor"],
                actor=actor,
                based_on_version=proposal["based_on_version"],
                adopt=False,
            )
            cur.execute(
                "UPDATE proposal SET applied_version = %s WHERE proposal_id = %s",
                (version_id, proposal["proposal_id"]),
            )
        entity = store.get_entity(cur, proposal["target_memory"])
        if EntityStatus(entity["status"]) is EntityStatus.PROVISIONAL:
            store.set_entity_status(
                cur,
                memory_id=proposal["target_memory"],
                target=EntityStatus.ACTIVE,
                actor=actor,
                reason="review settled the duplicate question",
            )
        store.set_active(
            cur,
            memory_id=proposal["target_memory"],
            version_id=version_id,
            actor=actor,
            reason=payload.get("reason"),
        )
        return

    if operation is ProposalOperation.CHANGE_STATUS:
        store.set_status(
            cur,
            version_id=payload["version_id"],
            target=VersionStatus(payload["status"]),
            actor=actor,
            reason=payload["reason"],
        )
        return

    if operation is ProposalOperation.RESTORE:
        store.set_active(
            cur,
            memory_id=proposal["target_memory"],
            version_id=payload["version_id"],
            actor=actor,
            reason=payload.get("reason"),
        )
        return

    if operation is ProposalOperation.RETYPE:
        store.set_type(
            cur,
            memory_id=proposal["target_memory"],
            target=MemoryType(payload["type"]),
            actor=actor,
            reason=payload["reason"],
        )
        return

    if operation is ProposalOperation.MERGE:
        store.merge_entities(
            cur,
            source=proposal["target_memory"],
            target=payload["into"],
            actor=actor,
            reason=payload["reason"],
            keep_active=payload.get("keep_active"),
        )
        return

    raise ProposalError(f"no approval path for operation {operation}")
