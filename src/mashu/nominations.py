"""Manage candidate memories awaiting human review."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from mashu import approvals, capacity, config, events, match, redact
from mashu.errors import MashuError, RefusedError

KINDS = ("incident", "rederivation", "user_explicit")

#: What the ledger records when an agent carries an instruction in (5.1).
CLAIMED_WHAT = "a user instruction carried by an agent; admission records its approval source"

#: What an agent is told when the instruction it carries repeats something already withdrawn.
_TOMBSTONE_NOTE = (
    "this repeats retired memory; the candidate carries each retirement kind, reason, and "
    "successor or destination. Invalidated and legacy conflicts require an explicit instruction "
    "that addresses those conflicts before admission."
)


def validate_evidence(cur: psycopg.Cursor, evidence: list[UUID]) -> None:
    """Refuse evidence that names nothing, in Python before the trigger does."""
    if not evidence:
        raise MashuError("a nomination without evidence is a suggestion, and those are not kept")
    if any(item is None for item in evidence):
        raise MashuError("evidence contains a NULL element")
    cur.execute("SELECT ledger_id FROM ledger WHERE ledger_id = ANY(%s)", (list(evidence),))
    present = {row["ledger_id"] for row in cur.fetchall()}
    missing = [item for item in evidence if item not in present]
    if missing:
        named = ", ".join(str(item) for item in missing)
        raise MashuError(f"evidence names ledger rows that do not exist: {named}")


def create_nomination(
    cur: psycopg.Cursor,
    *,
    content: str,
    kind: str,
    evidence: list[UUID],
    actor: str,
    scope_id: UUID | None = None,
    conflicts: list[UUID] | None = None,
) -> dict[str, Any]:
    """File a candidate. The evidence array is why it is allowed to be one."""
    if kind not in KINDS:
        raise MashuError(f"unknown nomination kind '{kind}' (expected one of {', '.join(KINDS)})")
    validate_evidence(cur, list(evidence))
    cur.execute(
        """
        INSERT INTO nomination (content, scope_id, kind, evidence, conflicts, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (content, scope_id, kind, list(evidence), list(conflicts or []) or None, actor),
    )
    row = cur.fetchone()
    snapshot = _conflict_snapshot(cur, list(conflicts or []))
    cur.execute(
        "UPDATE nomination SET conflict_snapshot = %s WHERE nomination_id = %s RETURNING *",
        (Jsonb(snapshot), row["nomination_id"]),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_created",
        actor,
        nomination_id=row["nomination_id"],
        detail={
            "kind": kind,
            "version": row["version"],
            "evidence": [str(e) for e in evidence],
            "conflicts": [str(c) for c in conflicts or []],
        },
    )
    return row


def add_evidence(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    ledger_id: UUID,
    *,
    actor: str,
    conflicts: list[UUID] | None = None,
) -> dict[str, Any] | None:
    """Put a fresh pain under a candidate that is already waiting for it."""
    cur.execute("SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE", (nomination_id,))
    row = cur.fetchone()
    if row is None or row["status"] != "pending":
        return None

    fresh_evidence = ledger_id not in (row["evidence"] or [])
    known = set(row["conflicts"] or [])
    fresh_conflicts = [c for c in (conflicts or []) if c not in known]
    if not fresh_evidence and not fresh_conflicts:
        return row

    cur.execute(
        """
        UPDATE nomination
        SET evidence  = CASE WHEN %(fresh)s
                             THEN array_append(evidence, %(ledger)s) ELSE evidence END,
            conflicts = CASE WHEN %(added)s::uuid[] = '{}'::uuid[] THEN conflicts
                             ELSE coalesce(conflicts, '{}'::uuid[]) || %(added)s::uuid[] END,
            deferred_at = NULL
        WHERE nomination_id = %(id)s AND status = 'pending'
        RETURNING *
        """,
        {
            "fresh": fresh_evidence,
            "ledger": ledger_id,
            "added": fresh_conflicts,
            "id": nomination_id,
        },
    )
    if cur.rowcount != 1:
        return None
    updated = cur.fetchone()
    events.record(
        cur,
        "nomination_evidence_added",
        actor,
        nomination_id=nomination_id,
        ledger_id=ledger_id if fresh_evidence else None,
        detail={
            "evidence": len(updated["evidence"]),
            "conflicts": [str(c) for c in fresh_conflicts],
            "version": updated["version"],
        },
    )
    return updated


def refresh_conflicts(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    conflicts: list[UUID],
    *,
    actor: str,
) -> dict[str, Any] | None:
    """Replace the displayed conflict set with the current retired matches."""
    cur.execute("SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE", (nomination_id,))
    row = cur.fetchone()
    if row is None or row["status"] != "pending":
        return None
    current = list(dict.fromkeys(conflicts))
    snapshot = _conflict_snapshot(cur, current)
    if set(row["conflicts"] or []) == set(current) and row["conflict_snapshot"] == snapshot:
        return row
    cur.execute(
        "UPDATE nomination SET conflicts = %s, conflict_snapshot = %s "
        "WHERE nomination_id = %s RETURNING *",
        (current or None, Jsonb(snapshot), nomination_id),
    )
    updated = cur.fetchone()
    events.record(
        cur,
        "nomination_conflicts_refreshed",
        actor,
        nomination_id=nomination_id,
        detail={"conflicts": [str(item) for item in current], "version": updated["version"]},
    )
    return updated


def nominate_user_explicit(
    cur: psycopg.Cursor, *, content: str, actor: str, scope_id: UUID | None = None
) -> dict[str, Any]:
    """Carry an instruction an agent says it was given, as far as the queue."""
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, capacity.LOCK_PAIN)
    )

    threshold = config.match_threshold()

    # Insert the ledger row first so nominations can cite it.
    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, created_by)
        VALUES ('claimed', %s, %s, %s, %s)
        RETURNING ledger_id
        """,
        (CLAIMED_WHAT, content, scope_id, actor),
    )
    ledger_id = cur.fetchone()["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": "claimed"})

    result: dict[str, Any] = {
        "ledger_id": ledger_id,
        "nomination": None,
        "nomination_existing": False,
        "tombstone_conflict": False,
        "unchecked": verdict.unchecked,
    }
    if verdict.malformed:
        result["malformed"] = verdict.malformed

    # A retirement is not a veto on this path, it is something the reviewer has to be looking at.
    tombstones = match.similar_tombstones(cur, content, limit=None)
    conflicts = [row["memory_id"] for row in tombstones if row["score"] >= threshold]
    if conflicts:
        result["tombstone_conflict"] = True
        result["matches"] = {"tombstones": tombstones}
        result["note"] = _TOMBSTONE_NOTE

    waiting = match.similar_pending_nominations(cur, content, limit=1)
    if waiting and waiting[0]["score"] >= threshold:
        existing = add_evidence(
            cur, waiting[0]["nomination_id"], ledger_id, actor=actor, conflicts=conflicts
        )
        if existing is not None:
            existing = (
                refresh_conflicts(cur, existing["nomination_id"], conflicts, actor=actor)
                or existing
            )
            result["nomination"] = existing
            result["nomination_existing"] = True
            return result

    result["nomination"] = create_nomination(
        cur,
        content=content,
        kind="user_explicit",
        evidence=[ledger_id],
        actor=actor,
        scope_id=scope_id,
        conflicts=conflicts,
    )
    return result


def pending_nominations(
    cur: psycopg.Cursor, *, include_deferred: bool = True
) -> list[dict[str, Any]]:
    """The queue, oldest first, each candidate carrying the pains it rests on."""
    cur.execute(
        f"""
        SELECT n.*, s.name AS scope_name
        FROM nomination n LEFT JOIN scope s ON s.scope_id = n.scope_id
        WHERE n.status = 'pending'
        {"" if include_deferred else "AND n.deferred_at IS NULL"}
        ORDER BY n.created_at, n.nomination_id
        """
    )
    rows = cur.fetchall()
    for row in rows:
        row["evidence_rows"] = _evidence_rows(cur, row["evidence"])
        row["conflict_rows"] = conflict_rows(cur, row["conflicts"])
    return rows


def deferred_count(cur: psycopg.Cursor) -> int:
    """How many pending candidates the sitting is holding back."""
    cur.execute(
        "SELECT count(*) AS n FROM nomination WHERE status = 'pending' AND deferred_at IS NOT NULL"
    )
    return cur.fetchone()["n"]


def _evidence_rows(cur: psycopg.Cursor, evidence: list[UUID]) -> list[dict[str, Any]]:
    if not evidence:
        return []
    cur.execute(
        """
        SELECT ledger_id, kind, what, prevention, created_at
        FROM ledger WHERE ledger_id = ANY(%s)
        """,
        (list(evidence),),
    )
    by_id = {row["ledger_id"]: row for row in cur.fetchall()}
    return [by_id[e] for e in evidence if e in by_id]


def _conflict_snapshot(cur: psycopg.Cursor, conflicts: list[UUID]) -> list[dict[str, Any]]:
    rows = conflict_rows(cur, conflicts)
    return [
        {
            "memory_id": str(row["memory_id"]),
            "retirement_kind": row.get("retirement_kind") or "legacy",
            "retire_reason": row["retire_reason"],
            "superseded_by": str(row["superseded_by"]) if row.get("superseded_by") else None,
            "relocated_to_kind": row.get("relocated_to_kind"),
            "relocated_to_id": str(row["relocated_to_id"]) if row.get("relocated_to_id") else None,
        }
        for row in sorted(rows, key=lambda item: str(item["memory_id"]))
    ]


def current_conflict_ids(cur: psycopg.Cursor, content: str) -> list[UUID]:
    """Every retired Memory that currently needs to be shown beside this body."""
    return [
        row["memory_id"]
        for row in match.similar_tombstones(cur, content, limit=None)
        if row["score"] >= config.match_threshold()
    ]


def conflict_rows(cur: psycopg.Cursor, conflicts: list[UUID] | None) -> list[dict[str, Any]]:
    """The retirements this candidate walks back into, as reasons only."""
    if not conflicts:
        return []
    cur.execute(
        """
        SELECT memory_id, retire_reason, retired_at, retirement_kind, delivery,
               superseded_by, relocated_to_kind, relocated_to_id
        FROM memory WHERE memory_id = ANY(%s)
        """,
        (list(conflicts),),
    )
    by_id = {row["memory_id"]: row for row in cur.fetchall()}
    return [by_id[c] for c in conflicts if c in by_id]


def blocking_conflicts(rows: list[dict[str, Any]]) -> list[UUID]:
    """The retirements an admission may not walk past without an instruction addressing them."""
    return [
        row["memory_id"] for row in rows if row.get("retirement_kind") in ("invalidated", "legacy")
    ]


#: What both decision paths say when the row moved under them.
NOT_PENDING = "nomination is no longer pending"


def _require_pending(cur: psycopg.Cursor, nomination_id: UUID) -> dict[str, Any]:
    """The row, locked for this transaction, or an error saying it is decided."""
    cur.execute("SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE", (nomination_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no nomination {nomination_id}")
    if row["status"] != "pending":
        raise MashuError(f"{NOT_PENDING}: it was already {row['status']}")
    return row


def revise(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    *,
    content: str,
    actor: str,
) -> dict[str, Any]:
    """Replace a pending candidate's wording without deciding it."""
    nomination = _require_pending(cur, nomination_id)
    content = (content or "").strip()
    if not content:
        raise MashuError("a candidate cannot be empty")
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    conflict_ids = current_conflict_ids(cur, content)
    snapshot = _conflict_snapshot(cur, conflict_ids)
    cur.execute(
        """
        UPDATE nomination SET content = %s, conflicts = %s, conflict_snapshot = %s
        WHERE nomination_id = %s AND status = 'pending'
        RETURNING *
        """,
        (content, conflict_ids or None, Jsonb(snapshot), nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_revised",
        actor,
        nomination_id=nomination_id,
        detail={
            "from_chars": len(nomination["content"]),
            "to_chars": len(content),
            "conflicts": [str(item) for item in conflict_ids],
            "version": row["version"],
        },
    )
    return row


def admit(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    *,
    actor: str,
    delivery: str,
    expected_version: int,
    scope_id: UUID | None = None,
    scope_override: bool = False,
    guard_action: str | None = None,
    topic_id: UUID | None = None,
    content: str | None = None,
    approval: dict[str, Any],
    request_id: UUID,
    exclude_memory_id: UUID | None = None,
) -> dict[str, Any]:
    """Admit one read nomination version and replay its original response for the same request."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(request_id),))
    cur.execute("SELECT * FROM nomination WHERE admit_request_id = %s", (request_id,))
    replay = cur.fetchone()
    request = approvals.json_value(
        {
            "nomination_id": str(nomination_id),
            "expected_version": expected_version,
            "delivery": delivery,
            "scope_id": str(scope_id) if scope_id else None,
            "scope_override": scope_override,
            "guard_action": guard_action,
            "content": content,
            "exclude_memory_id": str(exclude_memory_id) if exclude_memory_id else None,
            "approval": approval,
        }
    )
    if topic_id is not None:
        # Absent otherwise, so requests stored before topics existed still replay.
        request["topic_id"] = str(topic_id)
    if replay is not None:
        if replay["nomination_id"] != nomination_id or replay["admit_request"] != request:
            raise MashuError("request_id was already used for a different Memory admission")
        return replay["admit_result"]

    nomination = _require_pending(cur, nomination_id)
    if nomination["version"] != expected_version:
        raise MashuError(
            "nomination version changed; read the current candidate before admitting it"
        )
    final = nomination["content"] if content is None else content
    home = scope_id if scope_override else nomination["scope_id"] if scope_id is None else scope_id

    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )

    current_conflicts = [
        row
        for row in match.similar_tombstones(cur, final, limit=None)
        if row["score"] >= config.match_threshold()
    ]
    current_ids = {row["memory_id"] for row in current_conflicts}
    current_snapshot = _conflict_snapshot(cur, [row["memory_id"] for row in current_conflicts])
    if (
        current_ids != set(nomination["conflicts"] or [])
        or current_snapshot != nomination["conflict_snapshot"]
    ):
        raise MashuError(
            "retirement conflicts changed since this nomination was read; read the updated "
            "candidate and confirm it again"
        )
    approval_source = approvals.validate(
        approval, required_conflicts=blocking_conflicts(current_conflicts)
    )

    from mashu import memories

    memories.check_delivery(delivery, home, guard_action, topic_id)
    if delivery != "guard":
        guard_action = None
    if delivery == "topic":
        home = memories.topic_home(cur, delivery, topic_id)

    verdict = redact.check(final)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    active = [
        row
        for row in match.similar_active_memories(cur, final)
        if row["score"] >= config.match_threshold() and row["memory_id"] != exclude_memory_id
    ]
    if active:
        raise MashuError(
            "a similar active Memory already exists; resolve that Memory instead of admitting "
            "another copy"
        )
    admission = capacity.check_admission(
        cur,
        content=final,
        delivery=delivery,
        scope_id=home,
        exclude_memory_id=exclude_memory_id,
        topic_id=topic_id,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        INSERT INTO memory
            (content, scope_id, delivery, guard_action, topic_id, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (final, home, delivery, guard_action, topic_id, list(nomination["evidence"]), actor),
    )
    memory = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory["memory_id"], final, actor, f"admitted from nomination {nomination_id}"),
    )
    cur.execute(
        """
        UPDATE nomination
        SET status = 'admitted', memory_id = %s, decided_by = %s, decided_at = now(),
            approval_source = %s, admit_request_id = %s, admit_request = %s,
            admit_result = %s
        WHERE nomination_id = %s AND status = 'pending'
        """,
        (
            memory["memory_id"],
            actor,
            Jsonb(approval_source),
            request_id,
            Jsonb(request),
            Jsonb(approvals.json_value(memory)),
            nomination_id,
        ),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)

    events.record(
        cur,
        "memory_created",
        actor,
        memory_id=memory["memory_id"],
        nomination_id=nomination_id,
        detail={
            "delivery": delivery,
            "approval_source": approval_source,
            **({"topic_id": str(topic_id)} if topic_id is not None else {}),
        },
    )
    events.record(
        cur,
        "nomination_admitted",
        actor,
        memory_id=memory["memory_id"],
        nomination_id=nomination_id,
        detail={"approval_source": approval_source, "request_id": str(request_id)},
    )
    return approvals.json_value(memory)


#: Why a one-call remember stopped at the queue instead of admitting.
_STOP_EXISTING = (
    "this content joined an already pending candidate whose body and evidence you have not "
    "read, so nothing was admitted. Read the returned nomination; if the user's instruction "
    "covers it, call memory_admit with its nomination_id and nomination version."
)
_STOP_CONFLICT = (
    "this content repeats Memory retired as invalidated or legacy, so nothing was admitted. "
    "Show the user the returned conflicts; if they address them, call memory_admit with the "
    "nomination_id, nomination version, conflict_ids, and conflict_instruction."
)


def remember_explicit(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    approval: dict[str, Any],
    request_id: UUID,
    scope_id: UUID | None = None,
    delivery: str | None = None,
    guard_action: str | None = None,
    topic_id: UUID | None = None,
) -> dict[str, Any]:
    """Nominate an instruction the agent carries and admit it at once when nothing needs reading.

    A stop keeps the ledger row and nomination, as memory_nominate would, so the agent can
    continue with an admission by id. Any other refusal propagates and writes nothing.
    """
    chosen_delivery = delivery or ("topic" if topic_id else "scope" if scope_id else "always")
    settings = {
        "actor": actor,
        "delivery": chosen_delivery,
        "scope_id": scope_id,
        "scope_override": True,
        "guard_action": guard_action,
        "topic_id": topic_id,
        "approval": approval,
        "request_id": request_id,
    }

    # The nomination a first call created is no longer pending, so a replay has to find it
    # before nominating again or it would file a second candidate for the same request.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(request_id),))
    cur.execute("SELECT * FROM nomination WHERE admit_request_id = %s", (request_id,))
    replay = cur.fetchone()
    if replay is not None:
        if replay["content"] != content:
            raise MashuError("request_id was already used for a different Memory admission")
        memory = admit(
            cur,
            replay["nomination_id"],
            expected_version=replay["admit_request"]["expected_version"],
            **settings,
        )
        verdict = redact.check(content)
        gate = {"unchecked": verdict.unchecked}
        if verdict.malformed:
            gate["malformed"] = verdict.malformed
        return _remembered(memory, replay, replay["evidence"][0], gate)

    nominated = nominate_user_explicit(cur, content=content, actor=actor, scope_id=scope_id)
    nomination = nominated["nomination"]
    if nominated["nomination_existing"]:
        return {"admitted": False, "stopped": _STOP_EXISTING, **nominated}
    blocking = blocking_conflicts(conflict_rows(cur, nomination["conflicts"]))
    if not approvals.acknowledges_conflicts(approval, blocking):
        return {"admitted": False, "stopped": _STOP_CONFLICT, **nominated}

    memory = admit(
        cur, nomination["nomination_id"], expected_version=nomination["version"], **settings
    )
    gate = {key: nominated[key] for key in ("unchecked", "malformed") if key in nominated}
    return _remembered(memory, nomination, nominated["ledger_id"], gate)


def _remembered(
    memory: dict[str, Any],
    nomination: dict[str, Any],
    ledger_id: UUID,
    gate: dict[str, Any],
) -> dict[str, Any]:
    # Built only from rows an admission freezes, so a replay answers exactly as the first call.
    return {
        "admitted": True,
        "memory": memory,
        "nomination_id": nomination["nomination_id"],
        "ledger_id": ledger_id,
        "conflicts": nomination["conflict_snapshot"],
        **gate,
    }


def decline(cur: psycopg.Cursor, nomination_id: UUID, *, actor: str, reason: str) -> dict[str, Any]:
    """Turn a candidate down, on the record."""
    _require_pending(cur, nomination_id)
    if not reason or not reason.strip():
        raise MashuError("declining needs a reason: the next report of this pain will read it")
    cur.execute(
        """
        UPDATE nomination
        SET status = 'declined', decision_reason = %s, decided_by = %s, decided_at = now()
        WHERE nomination_id = %s AND status = 'pending'
        RETURNING *
        """,
        (reason, actor, nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_declined",
        actor,
        nomination_id=nomination_id,
        detail={"reason": reason},
    )
    return row


def defer(cur: psycopg.Cursor, nomination_id: UUID, *, actor: str, reason: str) -> dict[str, Any]:
    """Put a candidate off, on the record, without deciding it."""
    _require_pending(cur, nomination_id)
    if not reason or not reason.strip():
        raise MashuError("putting a candidate off needs a reason: it is all the next reader gets")
    cur.execute(
        """
        UPDATE nomination
        SET deferred_at = now(), defer_reason = %s
        WHERE nomination_id = %s AND status = 'pending'
        RETURNING *
        """,
        (reason, nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_deferred",
        actor,
        nomination_id=nomination_id,
        detail={"reason": reason},
    )
    return row
