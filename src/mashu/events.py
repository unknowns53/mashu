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
