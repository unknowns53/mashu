"""Accidents, counted and split by cause (27.5, 30 段 D).

27.5 refuses to judge the switchover by feel. "It seems to be running" cannot
say whether the move worked, so what gets recorded is occurrences and the cause
of each — and the cause is the whole value, because each one leads somewhere
different. Bootstrap was short, so its contents get revisited. Nobody pulled,
so the calling requirement does. Nothing was captured, so the extractor does. A
tally without the split says only that something is wrong.

Nothing here is inferred. An accident is a judgement about a piece of work that
went wrong, which is not a thing the system can watch itself for: it would have
to know what the agent should have known. So this is a record a person writes,
and the only help it gives is refusing to take one without an account of what
happened.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import events
from mashu.errors import MashuError
from mashu.models import EventType, IncidentCause, IncidentKind

#: Which causes belong to which kind. The pairing is enforced in the schema as
#: well; keeping it here too is what lets the CLI say what the choices are
#: before the database refuses.
CAUSES: dict[IncidentKind, tuple[IncidentCause, ...]] = {
    IncidentKind.MISSED: (
        IncidentCause.BOOTSTRAP,
        IncidentCause.PULL,
        IncidentCause.CAPTURE,
    ),
    IncidentKind.STALE: (
        IncidentCause.FILTER,
        IncidentCause.INVENTORY,
        IncidentCause.MARKER,
    ),
}

#: Where each cause leads. 27.5 pairs a cause with the thing it sends you back
#: to; printing that beside the record is what stops the tally from being read
#: as a score.
LEADS_TO = {
    IncidentCause.BOOTSTRAP: "what the session opening carries, and its ceiling (21.2)",
    IncidentCause.PULL: "the calling requirement, and whether stage one is enough (6.1)",
    IncidentCause.CAPTURE: "the extractor: skip criteria, input budget, scope routing (16.3)",
    IncidentCause.FILTER: "the temporary context read filter — an outright bug (25.2)",
    IncidentCause.INVENTORY: "the sweep for rules with a shelf life (30 段 C, 'admin stale')",
    IncidentCause.MARKER: "recall in the marker path (27.4b, 'admin eval-retire')",
}


class IncidentError(MashuError):
    """An accident cannot be recorded as described."""


def record(
    cur: psycopg.Cursor,
    *,
    kind: IncidentKind,
    cause: IncidentCause,
    note: str,
    recorded_by: str,
    memory_id: UUID | None = None,
    scope_id: UUID | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Write down one accident, with the account 27.5 requires.

    The cause has to belong to the kind. They are separate axes: one is
    knowledge that existed and did not arrive, the other is knowledge that had
    been withdrawn and arrived anyway. Letting a cause cross between them would
    put the two failure directions in one column, which is exactly the blur the
    split exists to prevent.
    """
    kind = IncidentKind(kind)
    cause = IncidentCause(cause)
    if cause not in CAUSES[kind]:
        allowed = ", ".join(str(c) for c in CAUSES[kind])
        raise IncidentError(f"a {kind} accident is caused by one of: {allowed}")
    if not note or not note.strip():
        raise IncidentError(
            "say what happened. A row with no account cannot be sorted into a "
            "cause later, and the causes are what the count is for"
        )

    cur.execute(
        """
        INSERT INTO incident (kind, cause, memory_id, scope_id, note, recorded_by, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()))
        RETURNING *
        """,
        (str(kind), str(cause), memory_id, scope_id, note.strip(), recorded_by, occurred_at),
    )
    row = cur.fetchone()
    events.record(
        cur,
        EventType.INCIDENT_RECORDED,
        recorded_by,
        memory_id=memory_id,
        detail={"kind": str(kind), "cause": str(cause), "incident_id": str(row["incident_id"])},
    )
    return row


def listed(
    cur: psycopg.Cursor,
    *,
    kind: IncidentKind | None = None,
    since_days: int | None = None,
) -> list[dict[str, Any]]:
    """Accidents, newest first, with the title of whatever each one names."""
    cur.execute(
        """
        SELECT i.*, e.title, s.name AS scope_name
        FROM incident i
        LEFT JOIN memory_entity e ON e.memory_id = i.memory_id
        LEFT JOIN scope s ON s.scope_id = coalesce(i.scope_id, e.scope_id)
        WHERE (%(kind)s::text IS NULL OR i.kind = %(kind)s)
          AND (%(days)s::int IS NULL
               OR i.occurred_at > now() - make_interval(days => %(days)s))
        ORDER BY i.occurred_at DESC
        """,
        {"kind": str(kind) if kind else None, "days": since_days},
    )
    return cur.fetchall()


def tally(cur: psycopg.Cursor, *, since_days: int | None = None) -> list[dict[str, Any]]:
    """How many of each cause, which is the only form the count is useful in."""
    cur.execute(
        """
        SELECT kind, cause, count(*) AS n, max(occurred_at) AS latest
        FROM incident
        WHERE (%(days)s::int IS NULL
               OR occurred_at > now() - make_interval(days => %(days)s))
        GROUP BY kind, cause
        ORDER BY kind, count(*) DESC
        """,
        {"days": since_days},
    )
    return cur.fetchall()
