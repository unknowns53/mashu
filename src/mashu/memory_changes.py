"""Propose and apply reviewed changes to existing memories."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from mashu import approvals, capacity, config, events, match, memories, nominations, redact
from mashu.errors import MashuError, RefusedError

OPERATIONS = ("retire", "replace", "restore")
RETIREMENT_KINDS = ("invalidated", "superseded", "out_of_scope", "relocated", "legacy")
BLOCKING_KINDS = ("invalidated", "legacy")


def _current(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any]:
    cur.execute(
        """
        SELECT m.*, r.revision_id
        FROM memory m
        LEFT JOIN LATERAL (
            SELECT revision_id FROM memory_revision
            WHERE memory_id = m.memory_id AND content = m.content
            ORDER BY created_at DESC, revision_id DESC LIMIT 1
        ) r ON true
        WHERE m.memory_id = %s
        """,
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no memory {memory_id}")
    if row["revision_id"] is None:
        raise MashuError(f"memory {memory_id} has no recorded revision")
    return row


def _current_conflicts(
    cur: psycopg.Cursor, content: str, *, exclude_memory_id: UUID | None = None
) -> list[dict[str, Any]]:
    rows = match.similar_tombstones(cur, content, exclude=exclude_memory_id, limit=None)
    return [row for row in rows if row["score"] >= config.match_threshold()]


def _conflict_snapshot(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _successor_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return approvals.json_value(
        {
            "version": row["version"],
            "content": row["content"],
            "scope_id": row["scope_id"],
            "kind": row["kind"],
            "evidence": row["evidence"],
            "conflicts": row["conflicts"] or [],
            "conflict_snapshot": row["conflict_snapshot"],
        }
    )


def _successor_settings(
    cur: psycopg.Cursor,
    target: dict[str, Any],
    requested: dict[str, Any] | None,
) -> dict[str, Any]:
    if requested is None:
        return {
            "delivery": target["delivery"],
            "scope_id": target["scope_id"],
            "guard_action": target["guard_action"],
        }
    if not isinstance(requested, dict) or set(requested) != {
        "delivery",
        "scope_id",
        "guard_action",
    }:
        raise MashuError(
            "successor_settings must specify delivery, scope_id, and guard_action together"
        )
    delivery = requested["delivery"]
    if delivery not in memories.DELIVERIES:
        raise MashuError(f"successor delivery must be one of {', '.join(memories.DELIVERIES)}")
    scope_id = requested["scope_id"]
    if scope_id is not None:
        try:
            scope_id = UUID(str(scope_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise MashuError("successor scope_id must be a Scope UUID or null") from error
        cur.execute("SELECT 1 FROM scope WHERE scope_id = %s", (scope_id,))
        if cur.fetchone() is None:
            raise MashuError(f"no successor scope {scope_id}")
    guard_action = requested["guard_action"]
    if delivery == "scope" and scope_id is None:
        raise MashuError("successor delivery 'scope' needs a scope_id")
    if delivery == "guard":
        if not isinstance(guard_action, str) or not guard_action.strip():
            raise MashuError("successor delivery 'guard' needs a guard_action")
        guard_action = guard_action.strip()
    elif guard_action is not None:
        raise MashuError("successor guard_action only applies to guard delivery")
    return {"delivery": delivery, "scope_id": scope_id, "guard_action": guard_action}


def _validate_evidence(cur: psycopg.Cursor, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(evidence, list) or not evidence:
        raise MashuError("a memory change needs at least one observation or evidence reference")
    cleaned: list[dict[str, Any]] = []
    for row in evidence:
        if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
            raise MashuError("evidence entries need a kind and a reference")
        kind = row["kind"]
        if kind in ("ledger", "trace"):
            try:
                reference = UUID(str(row.get("id")))
            except (ValueError, TypeError, AttributeError) as error:
                raise MashuError(f"{kind} evidence needs a valid id") from error
            table, key = ("ledger", "ledger_id") if kind == "ledger" else ("trace", "trace_id")
            cur.execute(f"SELECT 1 FROM {table} WHERE {key} = %s", (reference,))
            if cur.fetchone() is None:
                raise MashuError(f"{kind} evidence {reference} does not exist")
            entry = {"kind": kind, "id": str(reference)}
        elif kind == "artifact":
            reference = row.get("ref")
            if not isinstance(reference, str) or not reference.strip():
                raise MashuError("artifact evidence needs a non-empty ref")
            verdict = redact.check(reference)
            if not verdict.allowed:
                raise RefusedError(verdict.reason())
            entry = {"kind": kind, "ref": reference.strip()}
        else:
            raise MashuError("evidence kind must be ledger, trace, or artifact")
        observation = row.get("observation")
        if not isinstance(observation, str) or not observation.strip() or len(observation) > 2000:
            raise MashuError(
                "each evidence reference needs an observation of at most 2000 characters"
            )
        verdict = redact.check(observation)
        if not verdict.allowed:
            raise RefusedError(verdict.reason())
        entry["observation"] = observation.strip()
        cleaned.append(entry)
    return cleaned


def _validate_change(
    cur: psycopg.Cursor,
    *,
    target: dict[str, Any],
    operation: str,
    retirement_kind: str | None,
    retire_reason: str | None,
    successor_nomination_id: UUID | None,
    relocated_to_kind: str | None,
    relocated_to_id: UUID | None,
    restore_reason: str | None,
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise MashuError(f"operation must be one of {', '.join(OPERATIONS)}")
    if operation == "restore" and any(
        value is not None
        for value in (
            retirement_kind,
            retire_reason,
            successor_nomination_id,
            relocated_to_kind,
            relocated_to_id,
        )
    ):
        raise MashuError("restore only accepts restore_reason; retirement fields do not apply")
    if operation in ("retire", "replace"):
        if target["status"] != "active":
            raise MashuError(
                f"memory {target['memory_id']} is {target['status']}; retire and replace "
                "need active memory"
            )
        if not retire_reason or not retire_reason.strip():
            raise MashuError("retire_reason is required")
        verdict = redact.check(retire_reason)
        if not verdict.allowed:
            raise RefusedError(verdict.reason())
        if retirement_kind not in RETIREMENT_KINDS:
            raise MashuError(f"retirement_kind must be one of {', '.join(RETIREMENT_KINDS)}")
    if operation == "replace":
        if retirement_kind != "superseded" or successor_nomination_id is None:
            raise MashuError(
                "replace needs retirement_kind='superseded' and a successor nomination"
            )
        cur.execute(
            "SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE",
            (successor_nomination_id,),
        )
        successor = cur.fetchone()
        if successor is None or successor["status"] != "pending":
            raise MashuError("replacement needs a pending successor nomination")
        return successor
    if successor_nomination_id is not None:
        raise MashuError("successor_nomination_id is only valid for replace")
    if operation == "retire" and retirement_kind == "superseded":
        raise MashuError("use replace to retire a memory as superseded")
    if operation == "retire" and retirement_kind == "relocated":
        if relocated_to_kind != "temporary_context" or relocated_to_id is None:
            raise MashuError("relocated retirement needs temporary_context and its id")
        cur.execute("SELECT 1 FROM temporary_context WHERE context_id = %s", (relocated_to_id,))
        if cur.fetchone() is None:
            raise MashuError(f"no temporary context {relocated_to_id}")
    elif relocated_to_kind is not None or relocated_to_id is not None:
        raise MashuError("a relocation destination is only valid for relocated retirement")
    if operation == "restore":
        if target["status"] != "retired":
            raise MashuError("restore needs a retired memory")
        if not restore_reason or not restore_reason.strip():
            raise MashuError("restore_reason is required")
        verdict = redact.check(restore_reason)
        if not verdict.allowed:
            raise RefusedError(verdict.reason())
    elif restore_reason is not None:
        raise MashuError("restore_reason is only valid for restore")
    return {}


def propose(
    cur: psycopg.Cursor,
    *,
    target_memory_id: UUID,
    target_revision_id: UUID,
    target_updated_at: datetime,
    operation: str,
    evidence: list[dict[str, Any]],
    actor: str,
    retirement_kind: str | None = None,
    retire_reason: str | None = None,
    successor_nomination_id: UUID | None = None,
    successor_nomination_version: int | None = None,
    successor_settings: dict[str, Any] | None = None,
    relocated_to_kind: str | None = None,
    relocated_to_id: UUID | None = None,
    restore_reason: str | None = None,
    change_id: UUID | None = None,
) -> dict[str, Any]:
    """Propose against the target and successor versions that were read."""
    current = None
    if change_id is not None:
        cur.execute("SELECT * FROM memory_change WHERE change_id = %s FOR UPDATE", (change_id,))
        current = cur.fetchone()
        if current is None or current["status"] != "pending":
            raise MashuError("only a pending memory change can be updated")
        if current["target_memory_id"] != target_memory_id:
            raise MashuError("updating a proposal cannot change its target Memory")
    target = _current(cur, target_memory_id)
    if target["revision_id"] != target_revision_id or target["updated_at"] != target_updated_at:
        raise MashuError("target revision changed; read the current Memory before proposing")
    successor = _validate_change(
        cur,
        target=target,
        operation=operation,
        retirement_kind=retirement_kind,
        retire_reason=retire_reason,
        successor_nomination_id=successor_nomination_id,
        relocated_to_kind=relocated_to_kind,
        relocated_to_id=relocated_to_id,
        restore_reason=restore_reason,
    )
    if operation == "replace":
        if successor_nomination_version is None:
            raise MashuError("replace needs the successor nomination version that was read")
        if successor["version"] != successor_nomination_version:
            raise MashuError(
                "successor nomination version changed; read the current candidate before proposing"
            )
        successor_settings = _successor_settings(cur, target, successor_settings)
        successor_snapshot = _successor_snapshot(successor)
    else:
        if successor_nomination_version is not None:
            raise MashuError("successor_nomination_version only applies to replace")
        if successor_settings is not None:
            raise MashuError("successor_settings only applies to replace")
        successor_snapshot = None
    basis = _validate_evidence(cur, evidence)
    conflicts = (
        _current_conflicts(cur, successor["content"], exclude_memory_id=target_memory_id)
        if operation == "replace"
        else _current_conflicts(cur, target["content"], exclude_memory_id=target_memory_id)
        if operation == "restore"
        else []
    )
    conflict_ids = [row["memory_id"] for row in conflicts]
    conflict_snapshot = _conflict_snapshot(conflicts)
    if operation == "replace" and (
        set(successor.get("conflicts") or []) != set(conflict_ids)
        or successor.get("conflict_snapshot") != conflict_snapshot
    ):
        raise MashuError(
            "successor nomination retirement conflicts changed; refresh and reread the candidate"
        )

    if change_id is None:
        cur.execute(
            """
            INSERT INTO memory_change
                (target_memory_id, target_revision_id, target_updated_at, operation,
                 retirement_kind, retire_reason, relocated_to_kind, relocated_to_id,
                 successor_nomination_id, successor_snapshot, successor_delivery,
                 successor_scope_id, successor_guard_action, restore_reason, evidence,
                 conflict_ids, conflict_snapshot, proposed_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                target_memory_id,
                target_revision_id,
                target["updated_at"],
                operation,
                retirement_kind,
                retire_reason,
                relocated_to_kind,
                relocated_to_id,
                successor_nomination_id,
                Jsonb(successor_snapshot) if successor_snapshot is not None else None,
                successor_settings["delivery"] if successor_settings else None,
                successor_settings["scope_id"] if successor_settings else None,
                successor_settings["guard_action"] if successor_settings else None,
                restore_reason,
                Jsonb(basis),
                conflict_ids,
                Jsonb(conflict_snapshot),
                actor,
            ),
        )
    else:
        cur.execute(
            """
            UPDATE memory_change
            SET target_revision_id = %s, target_updated_at = %s, operation = %s,
                retirement_kind = %s, retire_reason = %s, relocated_to_kind = %s,
                relocated_to_id = %s, successor_nomination_id = %s,
                successor_snapshot = %s, successor_delivery = %s, successor_scope_id = %s,
                successor_guard_action = %s, restore_reason = %s, evidence = %s,
                conflict_ids = %s, conflict_snapshot = %s, proposed_by = %s,
                proposed_at = now(), version = version + 1
            WHERE change_id = %s AND status = 'pending'
            RETURNING *
            """,
            (
                target_revision_id,
                target["updated_at"],
                operation,
                retirement_kind,
                retire_reason,
                relocated_to_kind,
                relocated_to_id,
                successor_nomination_id,
                Jsonb(successor_snapshot) if successor_snapshot is not None else None,
                successor_settings["delivery"] if successor_settings else None,
                successor_settings["scope_id"] if successor_settings else None,
                successor_settings["guard_action"] if successor_settings else None,
                restore_reason,
                Jsonb(basis),
                conflict_ids,
                Jsonb(conflict_snapshot),
                actor,
                change_id,
            ),
        )
    row = cur.fetchone()
    events.record(
        cur,
        "memory_change_proposed",
        actor,
        memory_id=target_memory_id,
        nomination_id=successor_nomination_id,
        detail={
            "change_id": str(row["change_id"]),
            "operation": operation,
            "target_revision_id": str(target_revision_id),
            "version": row["version"],
            "retirement_kind": retirement_kind,
            "retire_reason": retire_reason,
            "restore_reason": restore_reason,
            "successor_nomination_id": str(successor_nomination_id)
            if successor_nomination_id
            else None,
            "successor_snapshot": successor_snapshot,
            "successor_settings": approvals.json_value(successor_settings),
            "relocated_to_kind": relocated_to_kind,
            "relocated_to_id": str(relocated_to_id) if relocated_to_id else None,
            "conflict_ids": [str(item) for item in conflict_ids],
            "conflict_snapshot": conflict_snapshot,
            "evidence": basis,
        },
    )
    return _detail(cur, row)


def _detail(cur: psycopg.Cursor, row: dict[str, Any]) -> dict[str, Any]:
    target = memories.memory_details(cur, row["target_memory_id"])
    row["target"] = target
    row["conflicts"] = nominations.conflict_rows(cur, row["conflict_ids"])
    if row["successor_nomination_id"] is not None:
        cur.execute(
            "SELECT * FROM nomination WHERE nomination_id = %s", (row["successor_nomination_id"],)
        )
        row["successor"] = cur.fetchone()
        row["successor_changed"] = (
            _successor_snapshot(row["successor"]) != row["successor_snapshot"]
        )
    else:
        row["successor"] = None
    return row


def get(cur: psycopg.Cursor, change_id: UUID) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM memory_change WHERE change_id = %s", (change_id,))
    row = cur.fetchone()
    return _detail(cur, row) if row else None


def pending(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT * FROM memory_change WHERE status = 'pending' ORDER BY proposed_at, change_id"
    )
    return [_detail(cur, row) for row in cur.fetchall()]


def _blocking_conflicts(rows: list[dict[str, Any]]) -> list[UUID]:
    return [row["memory_id"] for row in rows if row.get("retirement_kind") in BLOCKING_KINDS]


def _assert_conflicts_unchanged(
    cur: psycopg.Cursor, row: dict[str, Any], content: str
) -> list[dict[str, Any]]:
    current = _current_conflicts(cur, content, exclude_memory_id=row["target_memory_id"])
    current_ids = {item["memory_id"] for item in current}
    if current_ids != set(row["conflict_ids"] or []):
        raise MashuError(
            "retirement conflicts changed after this proposal was read; read the conflicts and "
            "update the proposal before applying it"
        )
    if _conflict_snapshot(current) != row["conflict_snapshot"]:
        raise MashuError(
            "retirement conflict details changed after this proposal was read; read the current "
            "reasons and update the proposal before applying it"
        )
    return current


def apply(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    version: int,
    request_id: UUID,
    approval: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Apply the exact displayed proposal once, atomically with its evidence and event."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(request_id),))
    cur.execute("SELECT * FROM memory_change WHERE apply_request_id = %s", (request_id,))
    reused = cur.fetchone()
    request = approvals.json_value(
        {"change_id": str(change_id), "version": version, "approval": approval}
    )
    if reused is not None:
        if reused["change_id"] != change_id or reused["apply_request"] != request:
            raise MashuError("request_id was already used for a different memory change request")
        return reused["applied_result"]

    cur.execute("SELECT * FROM memory_change WHERE change_id = %s FOR UPDATE", (change_id,))
    change = cur.fetchone()
    if change is None:
        raise MashuError(f"no memory change {change_id}")
    if change["status"] != "pending":
        raise MashuError(f"memory change is already {change['status']}")
    if change["version"] != version:
        raise MashuError("memory change version changed; read the current proposal before applying")
    cur.execute(
        "SELECT * FROM memory WHERE memory_id = %s FOR UPDATE", (change["target_memory_id"],)
    )
    target = cur.fetchone()
    if target is None:
        raise MashuError("target Memory no longer exists")
    fresh = _current(cur, target["memory_id"])
    if (
        fresh["revision_id"] != change["target_revision_id"]
        or fresh["updated_at"] != change["target_updated_at"]
    ):
        raise MashuError(
            "target Memory changed after this proposal was read; read it and update the proposal"
        )

    conflict_rows: list[dict[str, Any]] = []
    reversal_required = (
        change["operation"] == "restore" and target["retirement_kind"] in BLOCKING_KINDS
    )
    if change["operation"] == "replace":
        cur.execute(
            "SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE",
            (change["successor_nomination_id"],),
        )
        successor = cur.fetchone()
        if successor is None or successor["status"] != "pending":
            raise MashuError("replacement nomination is no longer pending")
        if _successor_snapshot(successor) != change["successor_snapshot"]:
            raise MashuError(
                "successor nomination changed after this proposal was read; read it and update "
                "the replacement proposal"
            )
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
        )
        conflict_rows = _assert_conflicts_unchanged(cur, change, successor["content"])
    elif change["operation"] == "restore":
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
        )
        conflict_rows = _assert_conflicts_unchanged(cur, change, target["content"])
        active = match.similar_active_memories(cur, target["content"])
        duplicates = [item for item in active if item["score"] >= config.match_threshold()]
        if duplicates:
            raise MashuError("a similar active Memory already exists; resolve that Memory first")

    approval_source = approvals.validate(
        approval,
        required_conflicts=_blocking_conflicts(conflict_rows),
        reversal_required=reversal_required,
    )

    result: dict[str, Any]
    if change["operation"] == "retire":
        retired = memories.retire(
            cur,
            target["memory_id"],
            reason=change["retire_reason"],
            retirement_kind=change["retirement_kind"],
            actor=actor,
            superseded_by=change["superseded_by"],
            relocated_to_kind=change["relocated_to_kind"],
            relocated_to_id=change["relocated_to_id"],
            approval_source=approval_source,
        )
        result = {"operation": "retire", "memory": retired}
    elif change["operation"] == "replace":
        admitted = nominations.admit(
            cur,
            change["successor_nomination_id"],
            actor=actor,
            delivery=change["successor_delivery"],
            expected_version=change["successor_snapshot"]["version"],
            scope_id=change["successor_scope_id"],
            scope_override=True,
            guard_action=change["successor_guard_action"],
            approval=approval_source,
            request_id=request_id,
            exclude_memory_id=target["memory_id"],
        )
        retired = memories.retire(
            cur,
            target["memory_id"],
            reason=change["retire_reason"],
            retirement_kind="superseded",
            actor=actor,
            superseded_by=UUID(str(admitted["memory_id"])),
            approval_source=approval_source,
        )
        result = {"operation": "replace", "memory": admitted, "retired": retired}
    else:
        restored = memories.restore(
            cur,
            target["memory_id"],
            reason=change["restore_reason"],
            actor=actor,
            approval_source=approval_source,
        )
        result = {"operation": "restore", "memory": restored}

    safe_result = approvals.json_value(result)
    cur.execute(
        """
        UPDATE memory_change
        SET status = 'applied', decided_by = %s, decided_at = now(), approval_source = %s,
            apply_request_id = %s, apply_request = %s, applied_result = %s,
            superseded_by = %s
        WHERE change_id = %s AND status = 'pending'
        RETURNING *
        """,
        (
            actor,
            Jsonb(approval_source),
            request_id,
            Jsonb(request),
            Jsonb(safe_result),
            UUID(str(result["memory"]["memory_id"])) if change["operation"] == "replace" else None,
            change_id,
        ),
    )
    if cur.rowcount != 1:
        raise MashuError("memory change was decided while being applied")
    events.record(
        cur,
        "memory_change_applied",
        actor,
        memory_id=target["memory_id"],
        nomination_id=change["successor_nomination_id"],
        detail={
            "change_id": str(change_id),
            "version": version,
            "operation": change["operation"],
            "approval_source": approval_source,
            "request_id": str(request_id),
            "result_memory_id": str(result["memory"]["memory_id"]),
            "successor_snapshot": change["successor_snapshot"]
            if change["operation"] == "replace"
            else None,
            "successor_settings": {
                "delivery": change["successor_delivery"],
                "scope_id": str(change["successor_scope_id"])
                if change["successor_scope_id"]
                else None,
                "guard_action": change["successor_guard_action"],
            }
            if change["operation"] == "replace"
            else None,
        },
    )
    return safe_result


def decide(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    status: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Decline or withdraw a proposal, retaining why it left the queue."""
    if status not in ("declined", "withdrawn"):
        raise MashuError("a memory change can be declined or withdrawn")
    if not reason or not reason.strip():
        raise MashuError(f"{status} needs a reason")
    verdict = redact.check(reason)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    cur.execute("SELECT * FROM memory_change WHERE change_id = %s FOR UPDATE", (change_id,))
    row = cur.fetchone()
    if row is None or row["status"] != "pending":
        raise MashuError("memory change is no longer pending")
    cur.execute(
        "UPDATE memory_change SET status = %s, decided_by = %s, decided_at = now(), "
        "decision_reason = %s "
        "WHERE change_id = %s RETURNING *",
        (status, actor, reason, change_id),
    )
    result = cur.fetchone()
    events.record(
        cur,
        f"memory_change_{status}",
        actor,
        memory_id=row["target_memory_id"],
        nomination_id=row["successor_nomination_id"],
        detail={"change_id": str(change_id), "version": row["version"], "reason": reason},
    )
    return result
