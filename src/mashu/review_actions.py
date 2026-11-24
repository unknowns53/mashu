"""Human review decisions shared by the terminal screen and the desktop app."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg

from mashu import memory_actions, nominations, temporary
from mashu.errors import MashuError


@dataclass(frozen=True)
class Changed:
    """An admission stopped because the candidate is no longer the one that was read.

    The transaction still commits, so refreshed retirement conflicts are kept.
    """

    #: "candidate" when its wording changed, "conflicts" when its retirement conflicts did.
    what: str
    #: The candidate as it now stands, or None when it has left the pending queue.
    current: dict[str, Any] | None


def _pending(cur: psycopg.Cursor, nomination_id: UUID) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in nominations.pending_nominations(cur)
            if item["nomination_id"] == nomination_id
        ),
        None,
    )


def admit(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    *,
    expected_version: int,
    delivery: str,
    topic: dict[str, Any] | None,
    conflict_ids: list[UUID],
    actor: str,
    project_name: str | None = None,
    project_names: Sequence[str] | None = None,
    content: str | None = None,
    why: str | None = None,
) -> dict[str, Any] | Changed:
    """Admit exactly the version shown to the person, after refreshing its conflicts.

    conflict_ids are the retired conflicts the person was shown and read past.
    When `project_names` or `project_name` is given for project delivery, delivers there
    instead of the candidate's own project. Optional `content` revises the wording before
    admitting, and optional `why` records a reason row in the same transaction.
    """
    from mashu import memories

    cur.execute(
        "SELECT status, version, content, project_id FROM nomination "
        "WHERE nomination_id = %s FOR UPDATE",
        (nomination_id,),
    )
    current = cur.fetchone()
    if current is None or current["status"] != "pending":
        raise MashuError(nominations.NOT_PENDING)
    if current["version"] != expected_version:
        return Changed("candidate", _pending(cur, nomination_id))

    revised_content = (content or "").strip() or None
    if revised_content is not None and revised_content != current["content"]:
        revised = nominations.revise(cur, nomination_id, content=revised_content, actor=actor)
        expected_version = revised["version"]
        new_blocking = nominations.blocking_conflicts(
            nominations.conflict_rows(cur, revised["conflicts"])
        )
        if set(new_blocking) - set(conflict_ids):
            return Changed("conflicts", _pending(cur, nomination_id))
        conflict_ids = new_blocking
    else:
        refreshed = nominations.refresh_conflicts(
            cur,
            nomination_id,
            nominations.current_conflict_ids(cur, current["content"]),
            actor=actor,
        )
        if refreshed is None:
            raise MashuError(nominations.NOT_PENDING)
        if refreshed["version"] != expected_version:
            return Changed("conflicts", _pending(cur, nomination_id))

    project_id: UUID | None = None
    project_override = False
    others: list[UUID] = []
    if delivery == "project":
        if project_names is not None:
            project_id, others = memory_actions.project_ids_named(
                cur, project_names, home=current["project_id"]
            )
            project_override = True
        elif project_name is not None:
            project_id = memory_actions.project_id_named(cur, project_name)
            project_override = True

    admitted = nominations.admit(
        cur,
        nomination_id,
        actor=actor,
        delivery=delivery,
        expected_version=expected_version,
        project_id=project_id,
        project_override=project_override,
        also_project_ids=others,
        topic_id=memory_actions.topic_id_for(cur, topic, actor=actor),
        approval={"kind": "user_direct", "conflict_ids": conflict_ids},
        request_id=uuid4(),
    )
    why_clean = (why or "").strip() or None
    if why_clean is not None:
        memories.add_reason(cur, UUID(str(admitted["memory_id"])), why=why_clean, actor=actor)
    return admitted


def admit_as_temporary(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    *,
    expected_version: int,
    days: float,
    actor: str,
    project_names: Sequence[str] | None = None,
    content: str | None = None,
) -> dict[str, Any] | Changed:
    """Turn a pending candidate into a temporary context instead of a permanent memory."""
    cur.execute(
        "SELECT status, version, content, project_id FROM nomination "
        "WHERE nomination_id = %s FOR UPDATE",
        (nomination_id,),
    )
    current = cur.fetchone()
    if current is None or current["status"] != "pending":
        raise MashuError(nominations.NOT_PENDING)
    if current["version"] != expected_version:
        return Changed("candidate", _pending(cur, nomination_id))
    body = (content or "").strip() or current["content"]
    if project_names is None:
        home, others = current["project_id"], []
    else:
        home, others = memory_actions.project_ids_named(
            cur, project_names, home=current["project_id"]
        )
    context = temporary.put_temporary(
        cur,
        content=body,
        actor=actor,
        days=days,
        project_id=home,
        also_project_ids=others,
    )
    until = context["expires_at"].date().isoformat()
    nominations.decline(
        cur,
        nomination_id,
        actor=actor,
        reason=f"recorded as temporary context {context['context_id']} until {until}",
    )
    return {"temporary": context}


def blocking_conflict_ids(row: dict[str, Any]) -> list[UUID]:
    """The retired conflicts a person must read before admitting this candidate."""
    return [
        item["memory_id"]
        for item in row.get("conflict_rows", [])
        if item.get("retirement_kind") in ("withdrawn", "legacy")
    ]
