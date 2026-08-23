"""The state changes the system is allowed to make on its own.

Everything a caller can reach from here is deterministic: it creates versions,
moves statuses along the transition table, moves the active pointer, and writes
the matching event rows. None of it decides whether a memory is true. That
judgement enters through a proposal and, for anything but an auto commit, a
human review.

Every function takes a cursor. The caller owns the transaction, which is how a
pointer switch and its status change stay atomic (specification 26).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import events
from mashu.errors import ConcurrentUpdateError, NotFoundError
from mashu.models import EventType, MemoryType, SourceType, VersionStatus
from mashu.transitions import check_can_be_active, check_transition


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def get_entity(cur: psycopg.Cursor, memory_id: UUID, *, lock: bool = False) -> dict:
    """One entity row. With lock, hold it for the rest of the transaction.

    The lock is what stops two concurrent adoptions from each reading the same
    latest_version and both passing the optimistic check.
    """
    cur.execute(
        "SELECT * FROM memory_entity WHERE memory_id = %s" + (" FOR UPDATE" if lock else ""),
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no memory entity {memory_id}")
    return row


def get_version(cur: psycopg.Cursor, version_id: UUID) -> dict:
    """One version row."""
    cur.execute("SELECT * FROM memory_version WHERE version_id = %s", (version_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no memory version {version_id}")
    return row


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------
def create_scope(
    cur: psycopg.Cursor,
    *,
    name: str,
    actor: str,
    description: str | None = None,
) -> UUID:
    """Add a scope to the ledger. Only the user does this (specification 7)."""
    cur.execute(
        "INSERT INTO scope (name, description) VALUES (%s, %s) RETURNING scope_id",
        (name, description),
    )
    scope_id = cur.fetchone()["scope_id"]
    events.record(cur, EventType.SCOPE_CREATED, actor, detail={"name": name})
    return scope_id


def create_entity(
    cur: psycopg.Cursor,
    *,
    scope_id: UUID,
    type: MemoryType,
    title: str,
    content: str,
    source_type: SourceType,
    created_by: str,
    actor: str,
    adopt: bool,
    status: VersionStatus = VersionStatus.CANDIDATE,
    reason: str | None = None,
) -> tuple[UUID, UUID]:
    """Create an entity together with its first version.

    With adopt, the version becomes the active one straight away, which is what
    an auto commit does. Without it the version sits as a candidate the pointer
    does not target, waiting for review (specification 17).
    """
    cur.execute(
        """
        INSERT INTO memory_entity (scope_id, type, title)
        VALUES (%s, %s, %s) RETURNING memory_id
        """,
        (scope_id, str(MemoryType(type)), title),
    )
    memory_id = cur.fetchone()["memory_id"]
    events.record(
        cur,
        EventType.ENTITY_CREATED,
        actor,
        memory_id=memory_id,
        detail={"title": title, "type": str(MemoryType(type))},
    )

    version_id = _insert_version(
        cur,
        memory_id=memory_id,
        content=content,
        status=status,
        supersedes=None,
        reason=reason,
        source_type=source_type,
        created_by=created_by,
        actor=actor,
    )
    _set_latest(cur, memory_id, version_id)
    if adopt:
        _point_active_at(cur, memory_id=memory_id, version_id=version_id, actor=actor)
    return memory_id, version_id


def add_version(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    content: str,
    source_type: SourceType,
    created_by: str,
    actor: str,
    based_on_version: UUID | None,
    adopt: bool,
    status: VersionStatus = VersionStatus.CANDIDATE,
    reason: str | None = None,
) -> UUID:
    """Add a version to an existing entity.

    based_on_version is the optimistic lock (specification 24): it has to match
    what the entity currently calls latest, otherwise the caller wrote its
    proposal against a state that has since moved and has to read and propose
    again.
    """
    entity = get_entity(cur, memory_id, lock=True)
    if entity["latest_version"] != based_on_version:
        raise ConcurrentUpdateError(
            f"entity {memory_id} is now at {entity['latest_version']}, "
            f"but the change was written against {based_on_version}; "
            f"re-read and propose again"
        )

    previous_active = entity["active_version"]
    version_id = _insert_version(
        cur,
        memory_id=memory_id,
        content=content,
        status=status,
        supersedes=previous_active if adopt else None,
        reason=reason,
        source_type=source_type,
        created_by=created_by,
        actor=actor,
    )
    _set_latest(cur, memory_id, version_id)
    if adopt:
        _point_active_at(
            cur, memory_id=memory_id, version_id=version_id, actor=actor, reason=reason
        )
    return version_id


def set_status(
    cur: psycopg.Cursor,
    *,
    version_id: UUID,
    target: VersionStatus,
    actor: str,
    reason: str,
) -> None:
    """Move one version along the transition table.

    A reason is required. Specification 31 counts a change whose motive was not
    recorded as an untraceable one.

    If the version being moved is the active one and the new status cannot be
    active, the pointer is cleared rather than left dangling: the entity then
    has no current truth, which is the honest reading of disproving what was
    being relied on.
    """
    version = get_version(cur, version_id)
    current = VersionStatus(version["status"])
    target = VersionStatus(target)
    check_transition(current, target)

    entity = get_entity(cur, version["memory_id"], lock=True)

    cur.execute(
        "UPDATE memory_version SET status = %s WHERE version_id = %s",
        (str(target), version_id),
    )
    events.record(
        cur,
        EventType.STATUS_CHANGED,
        actor,
        memory_id=version["memory_id"],
        version_id=version_id,
        detail={"from": str(current), "to": str(target), "reason": reason},
    )

    if entity["active_version"] == version_id and target not in _ACTIVE_OK:
        _clear_active(cur, memory_id=version["memory_id"], actor=actor, reason=reason)


def set_active(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    version_id: UUID,
    actor: str,
    reason: str | None = None,
) -> None:
    """Adopt an existing version of this entity as the active one.

    The version that was active becomes superseded in the same transaction, so
    there is never a moment where two versions could both be read as current.
    """
    get_entity(cur, memory_id, lock=True)  # held for the rest of the transaction
    version = get_version(cur, version_id)
    if version["memory_id"] != memory_id:
        raise NotFoundError(
            f"version {version_id} belongs to {version['memory_id']}, not {memory_id}"
        )
    check_can_be_active(VersionStatus(version["status"]))
    _point_active_at(cur, memory_id=memory_id, version_id=version_id, actor=actor, reason=reason)


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------
_ACTIVE_OK = frozenset({VersionStatus.CANDIDATE, VersionStatus.COMPLETED})


def _insert_version(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    content: str,
    status: VersionStatus,
    supersedes: UUID | None,
    reason: str | None,
    source_type: SourceType,
    created_by: str,
    actor: str,
) -> UUID:
    cur.execute(
        """
        INSERT INTO memory_version
            (memory_id, content, status, supersedes, reason,
             source_type, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING version_id
        """,
        (
            memory_id,
            content,
            str(VersionStatus(status)),
            supersedes,
            reason,
            str(SourceType(source_type)),
            created_by,
        ),
    )
    version_id = cur.fetchone()["version_id"]
    detail: dict[str, Any] = {"status": str(VersionStatus(status))}
    if supersedes is not None:
        detail["supersedes"] = str(supersedes)
    events.record(
        cur,
        EventType.VERSION_CREATED,
        actor,
        memory_id=memory_id,
        version_id=version_id,
        detail=detail,
    )
    return version_id


def _set_latest(cur: psycopg.Cursor, memory_id: UUID, version_id: UUID) -> None:
    cur.execute(
        "UPDATE memory_entity SET latest_version = %s WHERE memory_id = %s",
        (version_id, memory_id),
    )


def _point_active_at(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    version_id: UUID,
    actor: str,
    reason: str | None = None,
) -> None:
    """Move the pointer, retiring whatever it pointed at before."""
    entity = get_entity(cur, memory_id)
    previous = entity["active_version"]
    if previous == version_id:
        return

    if previous is not None:
        previous_status = VersionStatus(get_version(cur, previous)["status"])
        check_transition(previous_status, VersionStatus.SUPERSEDED)
        cur.execute(
            "UPDATE memory_version SET status = %s WHERE version_id = %s",
            (str(VersionStatus.SUPERSEDED), previous),
        )
        events.record(
            cur,
            EventType.STATUS_CHANGED,
            actor,
            memory_id=memory_id,
            version_id=previous,
            detail={
                "from": str(previous_status),
                "to": str(VersionStatus.SUPERSEDED),
                "reason": reason or "replaced by a newly adopted version",
            },
        )

    cur.execute(
        "UPDATE memory_entity SET active_version = %s WHERE memory_id = %s",
        (version_id, memory_id),
    )
    events.record(
        cur,
        EventType.ACTIVE_SWITCHED,
        actor,
        memory_id=memory_id,
        version_id=version_id,
        detail={"from": str(previous) if previous else None, "reason": reason},
    )


def _clear_active(cur: psycopg.Cursor, *, memory_id: UUID, actor: str, reason: str) -> None:
    """Leave the entity with no current truth."""
    cur.execute(
        "UPDATE memory_entity SET active_version = NULL WHERE memory_id = %s",
        (memory_id,),
    )
    events.record(
        cur,
        EventType.ACTIVE_SWITCHED,
        actor,
        memory_id=memory_id,
        detail={"to": None, "reason": reason},
    )
