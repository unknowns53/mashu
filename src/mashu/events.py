"""Append-only event log helpers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

_INSERT = """
INSERT INTO event_log (event_type, actor, memory_id, ledger_id, nomination_id, trace_id, detail)
VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING event_id
"""


def record(
    cur: psycopg.Cursor,
    event_type: str,
    actor: str,
    *,
    memory_id: UUID | None = None,
    ledger_id: UUID | None = None,
    nomination_id: UUID | None = None,
    trace_id: UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one event and return its id."""
    cur.execute(
        _INSERT,
        (
            event_type,
            actor,
            memory_id,
            ledger_id,
            nomination_id,
            trace_id,
            Jsonb(detail) if detail is not None else None,
        ),
    )
    return cur.fetchone()["event_id"]


def guard_outcomes(cur: psycopg.Cursor, *, days: int = 30) -> list[dict[str, Any]]:
    """For each open topic linked to an action, its denials and what the next call did."""
    cur.execute(
        """
        SELECT t.name, t.action,
               count(*) FILTER (WHERE e.event_type = 'guard_served') AS served,
               count(*) FILTER (WHERE e.event_type = 'guard_followed') AS followed,
               count(*) FILTER (
                   WHERE e.event_type = 'guard_followed' AND (e.detail ->> 'changed')::boolean
               ) AS changed
        FROM topic t
        LEFT JOIN event_log e
               ON e.detail ->> 'topic_id' = t.topic_id::text
              AND e.event_type IN ('guard_served', 'guard_followed')
              AND e.created_at >= now() - make_interval(days => %s)
        WHERE t.action IS NOT NULL AND t.archived_at IS NULL
        GROUP BY t.topic_id
        ORDER BY t.name
        """,
        (days,),
    )
    return cur.fetchall()
