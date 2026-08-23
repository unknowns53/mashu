"""What is true until a stated moment (specification 25.2).

This is not a Memory and deliberately shares none of its machinery. A Memory
holds knowledge and knowledge is retired by a judgement; what is here holds the
conditions of the moment, and conditions stop applying on their own.

Nothing expires by being moved. The read filter is the whole mechanism, so
there is no worker whose death would leave a stale item standing. That is the
argument that decided against putting an expiry on the version instead:
section 10 makes the active pointer the single truth, and a pointer aimed at an
expired version would be Active by definition and invalid in retrieval.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from mashu import events
from mashu.errors import MashuError
from mashu.models import EventType, SourceType

#: The longest window an agent may claim for itself (25.2). Anything further
#: out is a claim about how things are, wearing a window, and belongs in a
#: proposal where a person can look at it.
AGENT_MAX_WINDOW_DAYS = 14

#: What a session start is willing to carry, per scope (25.2).
BOOTSTRAP_ITEM_LIMIT = 10

KINDS = ("fact", "preference")


class ContextError(MashuError):
    """A temporary item cannot be written as asked."""


def put(
    cur: psycopg.Cursor,
    *,
    content: str,
    expires_at: datetime,
    kind: str,
    source_type: SourceType,
    created_by: str,
    actor: str,
    scope_id: UUID | None = None,
    source_reference: str | None = None,
    valid_from: datetime | None = None,
) -> dict[str, Any]:
    """Record a condition that stops applying at a known moment.

    No commit gate. The gate protects the indefinite knowledge state, and
    paying a review for something the clock will undo is out of proportion to a
    blast radius the expiry already bounds.

    An agent's window is capped. A person's is not: a person stating that
    something holds until March is stating a fact about their own arrangements,
    and there is nobody better placed to know.
    """
    kind = str(kind)
    if kind not in KINDS:
        raise ContextError(
            f"kind must be one of {', '.join(KINDS)}; a general short-term store is a "
            f"design change, not a wider enum (25.2)"
        )
    source_type = SourceType(source_type)

    cur.execute("SELECT now() AS now")
    start = valid_from or cur.fetchone()["now"]
    if expires_at <= start:
        raise ContextError(f"expires_at {expires_at} is not after {start}")

    if source_type is not SourceType.USER:
        limit = start + timedelta(days=AGENT_MAX_WINDOW_DAYS)
        if expires_at > limit:
            raise ContextError(
                f"an agent may claim at most {AGENT_MAX_WINDOW_DAYS} days; a longer "
                f"window is a claim about how things are and belongs in a proposal (25.2)"
            )

    cur.execute(
        """
        INSERT INTO temporary_context
            (scope_id, kind, content, valid_from, expires_at, source_type,
             source_reference, created_by)
        VALUES (%s, %s, %s, coalesce(%s, now()), %s, %s, %s, %s)
        RETURNING *
        """,
        (
            scope_id,
            kind,
            content,
            valid_from,
            expires_at,
            str(source_type),
            source_reference,
            created_by,
        ),
    )
    row = cur.fetchone()
    events.record(
        cur,
        EventType.CONTEXT_RECORDED,
        actor,
        detail={
            "context_id": str(row["context_id"]),
            "scope_id": str(scope_id) if scope_id else None,
            "kind": kind,
            "expires_at": row["expires_at"].isoformat(),
            "source_type": str(source_type),
        },
    )
    return row


def live(
    cur: psycopg.Cursor,
    *,
    scopes: list[UUID] | None = None,
    pushed_only: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """What applies right now, soonest to expire first.

    pushed_only drops anything an agent wrote. Section 25.2 separates the right
    to write from the right to be pushed: a session start reaches every session
    including the ones that never asked, so a wrong window there costs more than
    the same window sitting in a search result, where only whoever looked pays.
    """
    cur.execute(
        """
        SELECT context_id, scope_id, kind, content, expires_at, source_type,
               source_reference, created_by, created_at
        FROM temporary_context
        WHERE revoked_at IS NULL
          AND valid_from <= now()
          AND now() < expires_at
          AND (%(scopes)s::uuid[] IS NULL
               OR scope_id IS NULL
               OR scope_id = ANY(%(scopes)s::uuid[]))
          AND (NOT %(pushed_only)s OR source_type = 'user')
        ORDER BY expires_at
        LIMIT %(limit)s
        """,
        {"scopes": scopes or None, "pushed_only": pushed_only, "limit": limit},
    )
    return cur.fetchall()


def revoke(cur: psycopg.Cursor, *, context_id: UUID, actor: str, reason: str) -> dict[str, Any]:
    """End a window early, when what it described stopped being true first."""
    cur.execute(
        """
        UPDATE temporary_context
        SET revoked_at = now(), revocation_reason = %s
        WHERE context_id = %s AND revoked_at IS NULL
        RETURNING *
        """,
        (reason, context_id),
    )
    row = cur.fetchone()
    if row is None:
        raise ContextError(f"no live temporary context {context_id}")
    events.record(
        cur,
        EventType.CONTEXT_REVOKED,
        actor,
        detail={"context_id": str(context_id), "reason": reason},
    )
    return row
