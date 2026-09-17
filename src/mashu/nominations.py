"""Manage candidate memories awaiting human review."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, match, redact
from mashu.errors import MashuError, RefusedError

KINDS = ("incident", "rederivation", "user_explicit")

#: What the ledger records when an agent carries an instruction in (5.1).
CLAIMED_WHAT = "a user instruction carried by an agent; confirm before it stands"

#: What an agent is told when the instruction it carries repeats something already withdrawn.
_TOMBSTONE_NOTE = (
    "this repeats a memory that was retired; the candidate carries the retire "
    "reason to the review screen, where a person reads both. Tell the user "
    "that the claim was withdrawn before, and why."
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
    events.record(
        cur,
        "nomination_created",
        actor,
        nomination_id=row["nomination_id"],
        detail={
            "kind": kind,
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
        },
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
    tombstones = match.similar_tombstones(cur, content)
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


def conflict_rows(cur: psycopg.Cursor, conflicts: list[UUID] | None) -> list[dict[str, Any]]:
    """The retirements this candidate walks back into, as reasons only."""
    if not conflicts:
        return []
    cur.execute(
        """
        SELECT memory_id, retire_reason, retired_at, delivery
        FROM memory WHERE memory_id = ANY(%s)
        """,
        (list(conflicts),),
    )
    by_id = {row["memory_id"]: row for row in cur.fetchall()}
    return [by_id[c] for c in conflicts if c in by_id]


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


def admit(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    *,
    actor: str,
    delivery: str,
    scope_id: UUID | None = None,
    guard_action: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Turn a candidate into a memory, carrying its evidence across."""
    nomination = _require_pending(cur, nomination_id)
    final = nomination["content"] if content is None else content
    home = nomination["scope_id"] if scope_id is None else scope_id

    if delivery not in ("always", "scope", "guard"):
        raise MashuError(f"unknown delivery '{delivery}'")
    if delivery == "scope" and home is None:
        raise MashuError("delivery 'scope' needs a scope to be delivered to")
    if delivery == "guard":
        if not guard_action:
            raise MashuError("delivery 'guard' needs the action it stands in front of")
    else:
        guard_action = None

    verdict = redact.check(final)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    admission = capacity.check_admission(cur, content=final, delivery=delivery, scope_id=home)
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        INSERT INTO memory (content, scope_id, delivery, guard_action, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (final, home, delivery, guard_action, list(nomination["evidence"]), actor),
    )
    memory = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory["memory_id"], final, actor, f"admitted from nomination {nomination_id}"),
    )
    cur.execute(
        """
        UPDATE nomination
        SET status = 'admitted', memory_id = %s, decided_by = %s, decided_at = now()
        WHERE nomination_id = %s AND status = 'pending'
        """,
        (memory["memory_id"], actor, nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)

    events.record(
        cur,
        "memory_created",
        actor,
        memory_id=memory["memory_id"],
        nomination_id=nomination_id,
        detail={"delivery": delivery},
    )
    events.record(
        cur,
        "nomination_admitted",
        actor,
        memory_id=memory["memory_id"],
        nomination_id=nomination_id,
    )
    return memory


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
