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

from mashu import events, store
from mashu.errors import DuplicatePendingError, NotFoundError, ProposalError
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


def find_duplicate_pending(
    cur: psycopg.Cursor,
    *,
    target_memory: UUID | None,
    scope_id: UUID | None = None,
    title: str | None = None,
) -> list[dict]:
    """Pending proposals that would change the same thing (specification 15.1).

    For a change to an existing entity the test is exact: same target. For a
    creation there is no target yet, so the test is on the title within the
    scope. Phase 2a replaces that with title_embedding similarity at the
    entity resolution threshold; until embeddings exist an exact match is what
    can be checked, and it catches the case the section is aimed at, an agent
    re-proposing what it already proposed.
    """
    if target_memory is not None:
        cur.execute(
            """
            SELECT proposal_id, actor, operation, payload, created_at,
                   EXTRACT(DAY FROM now() - created_at)::int AS days_pending
            FROM proposal
            WHERE status = 'pending' AND target_memory = %s
            ORDER BY created_at
            """,
            (target_memory,),
        )
        return cur.fetchall()

    if scope_id is None or title is None:
        return []
    cur.execute(
        """
        SELECT proposal_id, actor, operation, payload, created_at,
               EXTRACT(DAY FROM now() - created_at)::int AS days_pending
        FROM proposal
        WHERE status = 'pending'
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
    decision: grounds before conclusions, so the context is built once. Types
    the ordering does not name keep their arrival order after those three.

    Proposals with no session sit in one unnamed bundle rather than being
    dropped, so nothing can go missing from the queue by lacking provenance.
    """
    cur.execute(
        """
        SELECT p.proposal_id, p.session_id, p.actor, p.operation, p.target_memory,
               p.payload, p.created_at,
               EXTRACT(DAY FROM now() - p.created_at)::int AS days_pending,
               COALESCE(e.type, p.payload ->> 'type') AS memory_type,
               COALESCE(e.title, p.payload ->> 'title') AS title
        FROM proposal p
        LEFT JOIN memory_entity e ON e.memory_id = p.target_memory
        WHERE p.status = 'pending'
        ORDER BY p.session_id NULLS LAST, p.created_at
        """
    )
    rows = cur.fetchall()

    rank = {
        str(MemoryType.OBSERVATION): 0,
        str(MemoryType.INTERPRETATION): 1,
        str(MemoryType.DECISION): 2,
    }
    bundles: dict[UUID | None, list[dict]] = {}
    for row in rows:
        bundles.setdefault(row["session_id"], []).append(row)

    out = []
    for session_id, items in bundles.items():
        items.sort(key=lambda r: (rank.get(r["memory_type"], 3), r["created_at"]))
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
) -> dict[str, Any]:
    """Record a proposed change and take it as far as the gate allows.

    Returns the proposal row together with the gate's ruling, so the caller can
    tell an applied change from one that is now waiting.
    """
    operation = ProposalOperation(operation)

    if not allow_duplicate:
        existing = find_duplicate_pending(
            cur,
            target_memory=target_memory,
            scope_id=payload.get("scope_id"),
            title=payload.get("title"),
        )
        if existing:
            raise DuplicatePendingError(
                f"{len(existing)} pending proposal(s) already cover this; "
                f"look at them, and pass allow_duplicate to propose anyway",
                existing,
            )

    ruling = _rule(cur, operation, payload, target_memory)
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
    dormant, so that layer 3 of retrieval can tell the agent this was looked at
    and turned down. Deleting it would let the same proposal come back.
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
                target=VersionStatus.DORMANT,
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
            source_type=SourceType(payload["source_type"]),
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

    if operation is ProposalOperation.UPDATE_VERSION:
        if ruling.decision is CommitDecision.HUMAN_REVIEW:
            return None
        return store.add_version(
            cur,
            memory_id=proposal["target_memory"],
            content=payload["content"],
            source_type=SourceType(payload["source_type"]),
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
                source_type=SourceType(payload["source_type"]),
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
