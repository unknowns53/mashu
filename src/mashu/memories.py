"""Manage active memories, retirement reasons, and delivery scopes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, match, nominations, redact, topics
from mashu.errors import MashuError, RefusedError, RetiredConflictError

DELIVERIES = ("always", "scope", "topic")

_EXPLICIT_WHAT = "recorded by the user's own hand"


def check_delivery(
    delivery: str,
    scope_id: UUID | None,
    topic_id: UUID | None = None,
) -> None:
    """Refuse a delivery that lacks the one detail its route needs, or carries another's."""
    if delivery not in DELIVERIES:
        raise MashuError(f"unknown delivery '{delivery}' (expected one of {', '.join(DELIVERIES)})")
    if delivery == "scope" and scope_id is None:
        raise MashuError("delivery 'scope' needs a scope to be delivered to")
    if delivery == "topic" and topic_id is None:
        raise MashuError("delivery 'topic' needs the topic it is read with")
    if delivery != "topic" and topic_id is not None:
        raise MashuError("a topic only applies to delivery 'topic'")


def topic_home(cur: psycopg.Cursor, delivery: str, topic_id: UUID | None) -> UUID | None:
    """The scope a topic Memory takes from its topic, which must still be open."""
    if delivery != "topic" or topic_id is None:
        return None
    return topics.lock_open_topic(cur, topic_id)["scope_id"]


def remember(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    scope_id: UUID | None = None,
    delivery: str = "always",
    topic_id: UUID | None = None,
    acknowledged_conflicts: list[UUID] | None = None,
) -> dict[str, Any]:
    """Write a rule straight into the active set, with the writing as its evidence."""
    check_delivery(delivery, scope_id, topic_id)
    if delivery == "topic":
        scope_id = topic_home(cur, delivery, topic_id)

    verdict = redact.gate({"content": content})

    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )
    tombstones = match.similar_tombstones(cur, content, limit=None)
    retirement_matches = [row for row in tombstones if row["score"] >= config.match_threshold()]
    overruled = [
        row for row in retirement_matches if row.get("retirement_kind") in ("invalidated", "legacy")
    ]
    warnings = [row for row in retirement_matches if row not in overruled]
    expected_conflicts = {row["memory_id"] for row in overruled}
    if expected_conflicts != set(acknowledged_conflicts or []):
        if expected_conflicts:
            raise RetiredConflictError(
                "this repeats retired memories marked invalidated or legacy; read and "
                "acknowledge those retirement reasons before writing it back",
                overruled,
            )
        if acknowledged_conflicts:
            raise MashuError("no current invalidated or legacy conflicts match the acknowledgment")
    if overruled:
        approval_source = {
            "kind": "user_direct",
            "conflict_ids": sorted(map(str, expected_conflicts)),
        }
    else:
        approval_source = {"kind": "user_direct"}
    admission = capacity.check_admission(
        cur, content=content, delivery=delivery, scope_id=scope_id, topic_id=topic_id
    )
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
        INSERT INTO memory
            (content, scope_id, delivery, topic_id, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (content, scope_id, delivery, topic_id, [ledger_id], actor),
    )
    memory = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory["memory_id"], content, actor, _EXPLICIT_WHAT),
    )
    detail: dict[str, Any] = {"delivery": delivery, "approval_source": approval_source}
    if topic_id is not None:
        detail["topic_id"] = str(topic_id)
    if overruled:
        detail["overrides"] = [str(row["memory_id"]) for row in overruled]
    if retirement_matches:
        detail["retirement_conflicts"] = [
            {
                "memory_id": str(row["memory_id"]),
                "retirement_kind": row.get("retirement_kind") or "legacy",
            }
            for row in retirement_matches
        ]
    events.record(
        cur,
        "memory_created",
        actor,
        memory_id=memory["memory_id"],
        ledger_id=ledger_id,
        detail=detail,
    )
    return {
        **memory,
        **_gate_report(verdict),
        "overrides": overruled,
        "retirement_warnings": warnings,
    }


def _gate_report(verdict: redact.Verdict) -> dict[str, Any]:
    """What the caller is told about the gate itself, beside the result."""
    report: dict[str, Any] = {"unchecked": verdict.unchecked}
    if verdict.malformed:
        report["malformed"] = verdict.malformed
    return report


def get_memory(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM memory WHERE memory_id = %s", (memory_id,))
    return cur.fetchone()


def memory_details(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any] | None:
    """Read a Memory for an explicit management or change decision."""
    cur.execute(
        """
        SELECT m.*, s.name AS scope_name,
               t.name AS topic_name, t.trigger AS topic_trigger,
               r.revision_id AS current_revision_id,
               r.created_at AS revision_created_at
        FROM memory m
        LEFT JOIN scope s ON s.scope_id = m.scope_id
        LEFT JOIN topic t ON t.topic_id = m.topic_id
        LEFT JOIN LATERAL (
            SELECT revision_id, created_at FROM memory_revision
            WHERE memory_id = m.memory_id AND content = m.content
            ORDER BY created_at DESC, revision_id DESC LIMIT 1
        ) r ON true
        WHERE m.memory_id = %s
        """,
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    row["basis"] = nominations._evidence_rows(cur, row["evidence"])
    cur.execute(
        """
        SELECT revision_id, content, actor, note, created_at
        FROM memory_revision WHERE memory_id = %s ORDER BY created_at, revision_id
        """,
        (memory_id,),
    )
    row["revisions"] = cur.fetchall()
    cur.execute(
        """
        SELECT event_type, actor, detail, created_at
        FROM event_log
        WHERE memory_id = %s
          AND event_type IN ('memory_retired', 'memory_restored', 'memory_retirement_classified')
        ORDER BY event_id
        """,
        (memory_id,),
    )
    row["retirement_history"] = cur.fetchall()
    row["retirement"] = None
    if row["status"] == "retired":
        row["retirement"] = {
            "kind": row["retirement_kind"] or "legacy",
            "reason": row["retire_reason"],
            "retired_at": row["retired_at"],
            "superseded_by": row["superseded_by"],
            "relocated_to_kind": row["relocated_to_kind"],
            "relocated_to_id": row["relocated_to_id"],
        }
    return row


def _require_active(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any]:
    cur.execute("SELECT * FROM memory WHERE memory_id = %s FOR UPDATE", (memory_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no memory {memory_id}")
    if row["status"] != "active":
        raise MashuError(f"memory {memory_id} is {row['status']}, and only active rows change")
    return row


def retire(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    reason: str,
    actor: str,
    retirement_kind: str,
    superseded_by: UUID | None = None,
    relocated_to_kind: str | None = None,
    relocated_to_id: UUID | None = None,
    approval_source: dict[str, Any] | None = None,
    _legacy_compat: bool = False,
) -> dict[str, Any]:
    """Withdraw a rule, leaving the reason behind as the tombstone."""
    _require_active(cur, memory_id)
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )
    if not reason or not reason.strip():
        raise MashuError("retiring needs a reason: the reason is what later readers are given")
    if retirement_kind not in ("invalidated", "superseded", "out_of_scope", "relocated", "legacy"):
        raise MashuError("unknown retirement kind")
    if retirement_kind == "legacy" and not _legacy_compat:
        raise MashuError("legacy retirement kind is reserved for existing data and migration")
    if retirement_kind == "superseded":
        if superseded_by is None or superseded_by == memory_id:
            raise MashuError("superseded retirement needs a different successor Memory")
        cur.execute("SELECT status FROM memory WHERE memory_id = %s", (superseded_by,))
        successor = cur.fetchone()
        if successor is None:
            raise MashuError(f"no successor memory {superseded_by}")
        cur.execute(
            """
            WITH RECURSIVE successors(memory_id, superseded_by) AS (
                SELECT memory_id, superseded_by FROM memory WHERE memory_id = %s
                UNION ALL
                SELECT m.memory_id, m.superseded_by
                FROM memory m JOIN successors s ON m.memory_id = s.superseded_by
                WHERE s.superseded_by IS NOT NULL
            )
            SELECT EXISTS (SELECT 1 FROM successors WHERE memory_id = %s) AS cycle
            """,
            (superseded_by, memory_id),
        )
        if cur.fetchone()["cycle"]:
            raise MashuError("superseded_by would create a cycle")
    elif superseded_by is not None:
        raise MashuError("superseded_by is only valid for superseded retirement")
    if retirement_kind == "relocated":
        if relocated_to_kind != "temporary_context" or relocated_to_id is None:
            raise MashuError("relocated retirement needs a Temporary Context destination")
        cur.execute("SELECT 1 FROM temporary_context WHERE context_id = %s", (relocated_to_id,))
        if cur.fetchone() is None:
            raise MashuError(f"no temporary context {relocated_to_id}")
    elif relocated_to_kind is not None or relocated_to_id is not None:
        raise MashuError("relocation destination is only valid for relocated retirement")
    verdict = redact.gate({"retire_reason": reason})
    cur.execute(
        """
        UPDATE memory
        SET status = 'retired', retire_reason = %s, retired_at = now(), updated_at = now(),
            retirement_kind = %s, superseded_by = %s, relocated_to_kind = %s,
            relocated_to_id = %s
        WHERE memory_id = %s
        RETURNING *
        """,
        (reason, retirement_kind, superseded_by, relocated_to_kind, relocated_to_id, memory_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "memory_retired",
        actor,
        memory_id=memory_id,
        detail={
            "retirement_kind": retirement_kind,
            "reason": reason,
            "superseded_by": str(superseded_by) if superseded_by else None,
            "relocated_to_kind": relocated_to_kind,
            "relocated_to_id": str(relocated_to_id) if relocated_to_id else None,
            "approval_source": approval_source or {"kind": "user_direct"},
        },
    )
    return {**row, **_gate_report(verdict)}


def convert_to_temporary(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    days: float,
    actor: str,
) -> dict[str, Any]:
    """Replace a pushed memory with an expiring condition in one transaction."""
    from mashu import temporary

    current = _require_active(cur, memory_id)
    if current["delivery"] == "topic":
        raise MashuError(
            "a topic memory cannot become temporary because its topic's trigger would be lost"
        )
    scope_id = current["scope_id"] if current["delivery"] == "scope" else None
    context = temporary.put_temporary(
        cur,
        content=current["content"],
        actor=actor,
        days=days,
        scope_id=scope_id,
    )
    until = context["expires_at"].date().isoformat()
    retired = retire(
        cur,
        memory_id,
        reason=f"converted to temporary context until {until}",
        actor=actor,
        retirement_kind="relocated",
        relocated_to_kind="temporary_context",
        relocated_to_id=context["context_id"],
    )
    events.record(
        cur,
        "memory_converted_to_temporary",
        actor,
        memory_id=memory_id,
        detail={
            "context_id": str(context["context_id"]),
            "days": days,
            "delivery": current["delivery"],
            "retirement_kind": "relocated",
            "retire_reason": retired["retire_reason"],
        },
    )
    return {"memory": retired, "temporary": context}


def revise(
    cur: psycopg.Cursor, memory_id: UUID, *, content: str, actor: str, note: str | None = None
) -> dict[str, Any]:
    """Rewrite the body, keeping the old one in the revision history."""
    current = _require_active(cur, memory_id)
    redact.gate({"content": content})
    admission = capacity.check_admission(
        cur,
        content=content,
        delivery=current["delivery"],
        scope_id=current["scope_id"],
        exclude_memory_id=memory_id,
        topic_id=current["topic_id"],
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


def restore(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    reason: str,
    actor: str,
    approval_source: dict[str, Any],
) -> dict[str, Any]:
    """Restore a retired body through normal text, capacity, and conflict checks."""
    cur.execute("SELECT * FROM memory WHERE memory_id = %s FOR UPDATE", (memory_id,))
    current = cur.fetchone()
    if current is None or current["status"] != "retired":
        raise MashuError("restore needs a retired memory")
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )
    if not reason or not reason.strip():
        raise MashuError("restore needs the reason for reversing its retirement")
    verdict = redact.gate({"restored content": current["content"], "restore_reason": reason})

    conflicts = [
        row
        for row in match.similar_tombstones(cur, current["content"], limit=None)
        if row["memory_id"] != memory_id
        and row["score"] >= config.match_threshold()
        and row.get("retirement_kind") in ("invalidated", "legacy")
    ]
    expected = {str(row["memory_id"]) for row in conflicts}
    acknowledged = set(approval_source.get("conflict_ids", []))
    if expected != acknowledged:
        if expected:
            raise RetiredConflictError(
                "restore conflicts changed or need explicit acknowledgment", conflicts
            )
        if acknowledged:
            raise MashuError("no current invalidated or legacy conflicts match the acknowledgment")

    active = [
        row
        for row in match.similar_active_memories(cur, current["content"])
        if row["score"] >= config.match_threshold()
    ]
    if active:
        raise MashuError("a similar active Memory already exists; resolve that Memory first")
    home = current["scope_id"]
    if current["delivery"] == "topic":
        home = topic_home(cur, "topic", current["topic_id"])
    admission = capacity.check_admission(
        cur,
        content=current["content"],
        delivery=current["delivery"],
        scope_id=home,
        topic_id=current["topic_id"],
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])
    cur.execute(
        """
        UPDATE memory
        SET status = 'active', retire_reason = NULL, retired_at = NULL, updated_at = now(),
            retirement_kind = NULL, superseded_by = NULL,
            relocated_to_kind = NULL, relocated_to_id = NULL, scope_id = %s
        WHERE memory_id = %s AND status = 'retired'
        RETURNING *
        """,
        (home, memory_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "memory_restored",
        actor,
        memory_id=memory_id,
        detail={
            "reason": reason,
            "former_retirement_kind": current["retirement_kind"],
            "former_retire_reason": current["retire_reason"],
            "former_superseded_by": str(current["superseded_by"])
            if current["superseded_by"]
            else None,
            "former_relocated_to_kind": current["relocated_to_kind"],
            "former_relocated_to_id": str(current["relocated_to_id"])
            if current["relocated_to_id"]
            else None,
            "approval_source": approval_source,
        },
    )
    return {**row, **_gate_report(verdict)}


def set_delivery(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    delivery: str,
    actor: str,
    scope_id: UUID | None = None,
    clear_scope: bool = False,
    topic_id: UUID | None = None,
    approval_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move a rule between the opening, one scope's opening, and a topic."""
    current = _require_active(cur, memory_id)
    if clear_scope and scope_id is not None:
        raise MashuError("pass a scope or clear it, not both")
    home = None if clear_scope else (current["scope_id"] if scope_id is None else scope_id)
    check_delivery(delivery, home, topic_id)
    if delivery == "topic":
        home = topic_home(cur, delivery, topic_id)

    admission = capacity.check_admission(
        cur,
        content=current["content"],
        delivery=delivery,
        scope_id=home,
        exclude_memory_id=memory_id,
        topic_id=topic_id,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        UPDATE memory
        SET delivery = %s, scope_id = %s, topic_id = %s, updated_at = now()
        WHERE memory_id = %s
        RETURNING *
        """,
        (delivery, home, topic_id, memory_id),
    )
    row = cur.fetchone()
    detail: dict[str, Any] = {"from": current["delivery"], "to": delivery}
    if current["topic_id"] is not None or topic_id is not None:
        detail["from_topic_id"] = str(current["topic_id"]) if current["topic_id"] else None
        detail["to_topic_id"] = str(topic_id) if topic_id else None
    if approval_source is not None:
        detail["approval_source"] = approval_source
    events.record(cur, "delivery_changed", actor, memory_id=memory_id, detail=detail)
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
