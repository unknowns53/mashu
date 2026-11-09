"""Human review decisions shared by the terminal screen and the desktop app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg

from mashu import memory_actions, nominations
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
) -> dict[str, Any] | Changed:
    """Admit exactly the version shown to the person, after refreshing its conflicts.

    conflict_ids are the retired conflicts the person was shown and read past.
    """
    cur.execute(
        "SELECT status, version, content FROM nomination WHERE nomination_id = %s FOR UPDATE",
        (nomination_id,),
    )
    current = cur.fetchone()
    if current is None or current["status"] != "pending":
        raise MashuError(nominations.NOT_PENDING)
    if current["version"] != expected_version:
        return Changed("candidate", _pending(cur, nomination_id))
    refreshed = nominations.refresh_conflicts(
        cur, nomination_id, nominations.current_conflict_ids(cur, current["content"]), actor=actor
    )
    if refreshed is None:
        raise MashuError(nominations.NOT_PENDING)
    if refreshed["version"] != expected_version:
        return Changed("conflicts", _pending(cur, nomination_id))
    return nominations.admit(
        cur,
        nomination_id,
        actor=actor,
        delivery=delivery,
        expected_version=expected_version,
        topic_id=memory_actions.topic_id_for(cur, topic, actor=actor),
        approval={"kind": "user_direct", "conflict_ids": conflict_ids},
        request_id=uuid4(),
    )


def blocking_conflict_ids(row: dict[str, Any]) -> list[UUID]:
    """The retired conflicts a person must read before admitting this candidate."""
    return [
        item["memory_id"]
        for item in row.get("conflict_rows", [])
        if item.get("retirement_kind") in ("invalidated", "legacy")
    ]
