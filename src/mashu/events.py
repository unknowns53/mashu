"""Writing to the append-only log (specification 26).

There is no read-modify-write here and no update path, matching the trigger on
the table. Every call adds one row describing something that already happened
in the same transaction.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from mashu.models import EventType

_INSERT = """
INSERT INTO event_log (event_type, actor, proposal_id, memory_id, version_id, detail)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING event_id
"""


def record(
    cur: psycopg.Cursor,
    event_type: EventType,
    actor: str,
    *,
    proposal_id: UUID | None = None,
    memory_id: UUID | None = None,
    version_id: UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> int:
    """Append one event and return its id."""
    cur.execute(
        _INSERT,
        (
            str(EventType(event_type)),
            actor,
            proposal_id,
            memory_id,
            version_id,
            Jsonb(detail) if detail is not None else None,
        ),
    )
    return cur.fetchone()["event_id"]
