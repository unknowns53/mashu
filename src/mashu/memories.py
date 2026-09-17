"""Manage active memories, retirement reasons, and delivery scopes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, match, nominations, redact
from mashu.errors import MashuError, RefusedError, RetiredConflictError

DELIVERIES = ("always", "scope", "guard")

_EXPLICIT_WHAT = "recorded by the user's own hand"


def _check_delivery(delivery: str, scope_id: UUID | None, guard_action: str | None) -> None:
    if delivery not in DELIVERIES:
        raise MashuError(f"unknown delivery '{delivery}' (expected one of {', '.join(DELIVERIES)})")
    if delivery == "scope" and scope_id is None:
        raise MashuError("delivery 'scope' needs a scope to be delivered to")
    if delivery == "guard" and not guard_action:
        raise MashuError("delivery 'guard' needs the action it stands in front of")


def remember(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    scope_id: UUID | None = None,
    delivery: str = "always",
    guard_action: str | None = None,
    override_retired: bool = False,
) -> dict[str, Any]:
    """Write a rule straight into the active set, with the writing as its evidence."""
    _check_delivery(delivery, scope_id, guard_action)
    if delivery != "guard":
        guard_action = None

    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    tombstones = match.similar_tombstones(cur, content)
    overruled = [row for row in tombstones if row["score"] >= config.match_threshold()]
    if overruled and not override_retired:
        raise RetiredConflictError(
            f"this repeats {len(overruled)} retired memory/memories; read why it was "
            "withdrawn before writing it back",
            overruled,
        )
    admission = capacity.check_admission(cur, content=content, delivery=delivery, scope_id=scope_id)
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, created_by)
        VALUES ('explicit', %s, %s, %s, %s)
        RETURNING ledger_id
        """,
        (_EXPLICIT_WHAT, content, scope_id, actor),
    )
    ledger_id = cur.fetchone()["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": "explicit"})

    nominations.validate_evidence(cur, [ledger_id])
    cur.execute(
        """
        INSERT INTO memory (content, scope_id, delivery, guard_action, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (content, scope_id, delivery, guard_action, [ledger_id], actor),
    )
    memory = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory["memory_id"], content, actor, _EXPLICIT_WHAT),
    )
    detail: dict[str, Any] = {"delivery": delivery}
    if overruled:
        # Record the retirement override on the memory's creation event.
        detail["overrides"] = [str(row["memory_id"]) for row in overruled]
    events.record(
        cur,
        "memory_created",
        actor,
        memory_id=memory["memory_id"],
        ledger_id=ledger_id,
        detail=detail,
    )
    return {**memory, **_gate_report(verdict), "overrides": overruled}


def _gate_report(verdict: redact.Verdict) -> dict[str, Any]:
    """What the caller is told about the gate itself, beside the result."""
    report: dict[str, Any] = {"unchecked": verdict.unchecked}
    if verdict.malformed:
        report["malformed"] = verdict.malformed
    return report


def get_memory(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM memory WHERE memory_id = %s", (memory_id,))
    return cur.fetchone()


def _require_active(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any]:
    row = get_memory(cur, memory_id)
    if row is None:
        raise MashuError(f"no memory {memory_id}")
    if row["status"] != "active":
        raise MashuError(f"memory {memory_id} is {row['status']}, and only active rows change")
    return row


def retire(cur: psycopg.Cursor, memory_id: UUID, *, reason: str, actor: str) -> dict[str, Any]:
    """Withdraw a rule, leaving the reason behind as the tombstone."""
    _require_active(cur, memory_id)
    if not reason or not reason.strip():
        raise MashuError("retiring needs a reason: the reason is what later readers are given")
    verdict = redact.check(reason)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    cur.execute(
        """
        UPDATE memory
        SET status = 'retired', retire_reason = %s, retired_at = now(), updated_at = now()
        WHERE memory_id = %s
        RETURNING *
        """,
        (reason, memory_id),
    )
    row = cur.fetchone()
    events.record(cur, "memory_retired", actor, memory_id=memory_id, detail={"reason": reason})
    return {**row, **_gate_report(verdict)}


def revise(
    cur: psycopg.Cursor, memory_id: UUID, *, content: str, actor: str, note: str | None = None
) -> dict[str, Any]:
    """Rewrite the body, keeping the old one in the revision history."""
    current = _require_active(cur, memory_id)
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    admission = capacity.check_admission(
        cur,
        content=content,
        delivery=current["delivery"],
        scope_id=current["scope_id"],
        exclude_memory_id=memory_id,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        "UPDATE memory SET content = %s, updated_at = now() WHERE memory_id = %s RETURNING *",
        (content, memory_id),
    )
    row = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory_id, content, actor, note),
    )
    events.record(cur, "memory_revised", actor, memory_id=memory_id, detail={"note": note})
    return row


def set_delivery(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    delivery: str,
    actor: str,
    guard_action: str | None = None,
    scope_id: UUID | None = None,
    clear_scope: bool = False,
) -> dict[str, Any]:
    """Move a rule between the opening, one scope's opening, and the act gate."""
    current = _require_active(cur, memory_id)
    if clear_scope and scope_id is not None:
        raise MashuError("pass a scope or clear it, not both")
    home = None if clear_scope else (current["scope_id"] if scope_id is None else scope_id)
    _check_delivery(delivery, home, guard_action)
    if delivery != "guard":
        guard_action = None

    if delivery in ("always", "scope"):
        admission = capacity.check_admission(
            cur,
            content=current["content"],
            delivery=delivery,
            scope_id=home,
            exclude_memory_id=memory_id,
        )
        if not admission["ok"]:
            raise RefusedError(admission["refusal"])

    cur.execute(
        """
        UPDATE memory
        SET delivery = %s, guard_action = %s, scope_id = %s, updated_at = now()
        WHERE memory_id = %s
        RETURNING *
        """,
        (delivery, guard_action, home, memory_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "delivery_changed",
        actor,
        memory_id=memory_id,
        detail={"from": current["delivery"], "to": delivery, "action": guard_action},
    )
    return row


def always_memories(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT * FROM memory WHERE status = 'active' AND delivery = 'always'
        ORDER BY created_at, memory_id
        """
    )
    return cur.fetchall()


def scope_push_memories(cur: psycopg.Cursor, scope_id: UUID) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT * FROM memory
        WHERE status = 'active' AND delivery = 'scope' AND scope_id = %s
        ORDER BY created_at, memory_id
        """,
        (scope_id,),
    )
    return cur.fetchall()


def active_memories(cur: psycopg.Cursor, *, scope_id: UUID) -> list[dict[str, Any]]:
    """Everything alive in one scope, whatever route it takes to a session."""
    cur.execute(
        """
        SELECT * FROM memory
        WHERE status = 'active' AND scope_id = %s
        ORDER BY delivery, created_at, memory_id
        """,
        (scope_id,),
    )
    return cur.fetchall()


def guard_pins(
    cur: psycopg.Cursor, *, action: str, scope_id: UUID | None = None
) -> list[dict[str, Any]]:
    """The rules that stand in front of one act."""
    cur.execute(
        """
        SELECT * FROM memory
        WHERE status = 'active' AND delivery = 'guard' AND guard_action = %(action)s
          AND (scope_id IS NULL OR scope_id = %(scope)s::uuid)
        ORDER BY created_at, memory_id
        """,
        {"action": action, "scope": scope_id},
    )
    return cur.fetchall()
