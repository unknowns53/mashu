"""Human Memory and Memory change operations shared by the terminal screens and desktop app."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg

from mashu import memories, memory_changes, nominations, references, scopes, temporary, topics
from mashu.errors import ChangedSinceReadError, MashuError

#: The retirement kinds a person chooses; legacy belongs to data written before kinds existed.
RETIREMENT_KINDS = ("invalidated", "superseded", "out_of_scope", "relocated")

#: Conflicts that must be read, and named in the approval, before a change is applied.
BLOCKING_KINDS = ("invalidated", "legacy")


def scope_id_named(cur: psycopg.Cursor, name: str | None) -> UUID | None:
    """The named scope's id, or None for no scope."""
    return scopes.require_scope(cur, name)["scope_id"] if name else None


def topic_id_for(cur: psycopg.Cursor, choice: dict[str, Any] | None, *, actor: str) -> UUID | None:
    """The chosen topic's id, creating a described topic inside the caller's write.

    A choice is {"topic_id": id}, {"name": an open topic's name}, or a new topic described
    by name, trigger, and optionally scope_name.
    """
    if choice is None:
        return None
    if choice.get("topic_id") is not None:
        return choice["topic_id"]
    if choice.get("trigger") is None:
        return topics.require_topic(cur, choice["name"])["topic_id"]
    return topics.create_topic(
        cur,
        name=choice["name"],
        trigger=choice["trigger"],
        scope_id=scope_id_named(cur, choice.get("scope_name")),
        actor=actor,
    )["topic_id"]


def remember(
    cur: psycopg.Cursor,
    *,
    content: str,
    why: str | None,
    delivery: str,
    scope_name: str | None,
    topic: dict[str, Any] | None,
    acknowledged: list[UUID] | None,
    actor: str,
) -> dict[str, Any]:
    """Write a rule the person states directly.

    Raises RetiredConflictError naming the retirements it repeats until acknowledged lists
    exactly those, so the person reads why they were retired before writing it back.
    """
    return memories.remember(
        cur,
        content=content,
        actor=actor,
        scope_id=scope_id_named(cur, scope_name),
        delivery=delivery,
        topic_id=topic_id_for(cur, topic, actor=actor),
        acknowledged_conflicts=acknowledged,
        why=why,
    )


def set_delivery(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    delivery: str,
    scope_name: str | None,
    topic: dict[str, Any] | None,
    actor: str,
) -> dict[str, Any]:
    """Move a rule to another delivery; no scope named drops the one it had."""
    scope_id = scope_id_named(cur, scope_name)
    return memories.set_delivery(
        cur,
        memory_id,
        delivery=delivery,
        actor=actor,
        scope_id=scope_id,
        clear_scope=scope_id is None,
        topic_id=topic_id_for(cur, topic, actor=actor),
        approval_source={"kind": "user_direct"},
    )


def retire(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    kind: str,
    reason: str,
    successor: str | None,
    destination: str | None,
    actor: str,
) -> dict[str, Any]:
    """Retire a rule on the person's word; successor and destination may be id prefixes."""
    if kind not in RETIREMENT_KINDS:
        raise MashuError(f"retirement kind must be one of {', '.join(RETIREMENT_KINDS)}")
    superseded_by = (
        references.resolve_id(
            cur, successor, table="memory", column="memory_id", label="successor Memory"
        )
        if successor
        else None
    )
    relocated_to_id = (
        references.resolve_id(
            cur,
            destination,
            table="temporary_context",
            column="context_id",
            label="Temporary Context",
        )
        if destination
        else None
    )
    return memories.retire(
        cur,
        memory_id,
        reason=reason,
        actor=actor,
        retirement_kind=kind,
        superseded_by=superseded_by,
        relocated_to_kind="temporary_context" if relocated_to_id else None,
        relocated_to_id=relocated_to_id,
        approval_source={"kind": "user_direct"},
    )


def add_temporary(cur: psycopg.Cursor, *, content: str, days: float, actor: str) -> dict[str, Any]:
    """Record a temporary context for every scope; editing it can give it one."""
    return temporary.put_temporary(cur, content=content, actor=actor, days=days, scope_id=None)


def revise_temporary(
    cur: psycopg.Cursor, context_id: UUID, *, content: str, scope_name: str | None, actor: str
) -> dict[str, Any]:
    """Rewrite a standing temporary context; no scope named means every scope."""
    return temporary.revise(
        cur, context_id, content=content, scope_id=scope_id_named(cur, scope_name), actor=actor
    )


def pending_change(cur: psycopg.Cursor, change_id: UUID) -> dict[str, Any]:
    """One pending Memory change as the review screens show it."""
    found = next(
        (row for row in memory_changes.pending(cur) if row["change_id"] == change_id), None
    )
    if found is None:
        raise MashuError("memory change is no longer pending")
    return found


def blocking_conflict_ids(change: dict[str, Any]) -> list[UUID]:
    """The retired conflicts a person must read before applying this change."""
    return [
        item["memory_id"]
        for item in change.get("conflicts", [])
        if item.get("retirement_kind") in BLOCKING_KINDS
    ]


def apply_change(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    version: int,
    conflict_ids: list[UUID],
    actor: str,
) -> dict[str, Any]:
    """Apply exactly the version shown, approving the conflicts the person read.

    Raises ChangedSinceReadError when the change, its target, its successor, or its
    conflicts moved since; refresh_change then re-proposes it against what is current.
    """
    return memory_changes.apply(
        cur,
        change_id,
        version=version,
        request_id=uuid4(),
        approval={"kind": "user_direct", "conflict_ids": conflict_ids},
        actor=actor,
    )


def _repropose(
    cur: psycopg.Cursor,
    current: dict[str, Any],
    *,
    target: dict[str, Any],
    successor_version: int | None,
    actor: str,
    retire_reason: str | None,
    restore_reason: str | None,
) -> dict[str, Any]:
    return memory_changes.propose(
        cur,
        target_memory_id=current["target_memory_id"],
        target_revision_id=target.get("current_revision_id"),
        target_updated_at=target["updated_at"],
        operation=current["operation"],
        evidence=current["evidence"],
        actor=actor,
        retirement_kind=current["retirement_kind"],
        retire_reason=retire_reason,
        successor_nomination_id=current["successor_nomination_id"],
        successor_nomination_version=successor_version,
        successor_settings=memory_changes.requested_settings(current),
        relocated_to_kind=current["relocated_to_kind"],
        relocated_to_id=current["relocated_to_id"],
        restore_reason=restore_reason,
        change_id=current["change_id"],
    )


def refresh_change(cur: psycopg.Cursor, change_id: UUID, *, actor: str) -> dict[str, Any]:
    """Re-propose a change against its current target and conflicts after a stale refusal."""
    current = memory_changes.get(cur, change_id)
    if current is None or current["status"] != "pending":
        raise MashuError("memory change is no longer pending")
    target = memories.memory_details(cur, current["target_memory_id"])
    if target is None:
        raise MashuError("target Memory no longer exists")
    successor_version = None
    if current["operation"] == "replace":
        cur.execute(
            "SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE",
            (current["successor_nomination_id"],),
        )
        successor = cur.fetchone()
        if successor is None or successor["status"] != "pending":
            raise MashuError("replacement nomination is no longer pending")
        refreshed = nominations.refresh_conflicts(
            cur,
            current["successor_nomination_id"],
            nominations.current_conflict_ids(cur, successor["content"]),
            actor=actor,
        )
        if refreshed is None:
            raise MashuError("replacement nomination is no longer pending")
        successor_version = refreshed["version"]
    return _repropose(
        cur,
        current,
        target=target,
        successor_version=successor_version,
        actor=actor,
        retire_reason=current["retire_reason"],
        restore_reason=current["restore_reason"],
    )


def edit_change_reason(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    version: int,
    target_revision_id: UUID | None,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Reword a pending change's reason, as long as it and its target are as shown."""
    shown = pending_change(cur, change_id)
    if shown["operation"] == "redeliver":
        raise MashuError("a redeliver proposal has no reason to edit; its evidence says why")
    if not reason.strip():
        raise MashuError("the reason cannot be empty")
    if (
        shown["version"] != version
        or shown["target"].get("current_revision_id") != target_revision_id
    ):
        raise ChangedSinceReadError("the proposal or its target changed; read it again first")
    restoring = shown["operation"] == "restore"
    return _repropose(
        cur,
        shown,
        target=shown["target"],
        successor_version=shown["successor"]["version"]
        if shown["operation"] == "replace"
        else None,
        actor=actor,
        retire_reason=shown["retire_reason"] if restoring else reason,
        restore_reason=reason if restoring else shown["restore_reason"],
    )
