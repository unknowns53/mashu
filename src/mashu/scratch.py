"""Session-scoped working state (specification 25.1).

Scratch is not a bin. It is the first input to session end extraction: an agent
puts down what looks worth keeping as it goes, and the extraction reads that
rather than re-reading the whole transcript, which is what keeps the cost of
capturing bounded.

It is visible to one logical session and nothing else. Shown to a second
session it would be shared state with no provenance, no status and no scope,
which is the contamination the layer exists to prevent, rebuilt under a name
that sounds harmless.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg

from mashu.errors import MashuError, NotFoundError

KINDS = ("work_state", "candidate", "note")


class ScratchError(MashuError):
    """A scratch item cannot be written as asked."""


def register(
    cur: psycopg.Cursor,
    *,
    session_id: UUID,
    source_cli: str,
    external_session_id: str,
) -> None:
    """Tie an internal session to the CLI session it belongs to.

    Without this an unattended re-run cannot tell whether it has already read a
    transcript, so capture is not idempotent and the same knowledge arrives
    twice on every retry.
    """
    cur.execute(
        "UPDATE agent_session SET source_cli = %s, external_session_id = %s WHERE session_id = %s",
        (source_cli, external_session_id, session_id),
    )
    if cur.rowcount == 0:
        raise NotFoundError(f"no session {session_id}")


def put(
    cur: psycopg.Cursor,
    *,
    session_id: UUID,
    content: str,
    kind: str = "note",
    source_turn: str | None = None,
) -> dict[str, Any]:
    """Add one item. The column is JSONB but the value is a list, not a blob.

    A blob cannot be added to concurrently, cannot be read back item by item,
    and gives the extraction nothing to work from except a wall of text.
    """
    if kind not in KINDS:
        raise ScratchError(f"kind must be one of {', '.join(KINDS)}")
    if not content.strip():
        raise ScratchError("nothing to put")

    item = {
        "item_id": str(uuid4()),
        "kind": kind,
        "content": content.strip(),
        "source_turn": source_turn,
    }
    cur.execute(
        """
        UPDATE agent_session
        SET scratch = coalesce(scratch, '[]'::jsonb) || jsonb_build_array(
            jsonb_build_object(
                'item_id', %(item_id)s::text, 'kind', %(kind)s::text,
                'content', %(content)s::text, 'source_turn', %(source_turn)s::text,
                'created_at', now()
            ))
        WHERE session_id = %(session_id)s
        RETURNING scratch
        """,
        dict(item, session_id=session_id),
    )
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no session {session_id}")
    return row["scratch"][-1]


def get(cur: psycopg.Cursor, *, session_id: UUID) -> list[dict[str, Any]]:
    """Everything this session has put down. One session only, by construction."""
    cur.execute("SELECT scratch FROM agent_session WHERE session_id = %s", (session_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no session {session_id}")
    return row["scratch"] or []


def clear(cur: psycopg.Cursor, *, session_id: UUID) -> int:
    """Drop the scratch once extraction has succeeded (25.1).

    Only after success. A failed extraction keeps its input, or the retry has
    nothing to read and the session's observations are lost for good.
    """
    held = len(get(cur, session_id=session_id))
    cur.execute("UPDATE agent_session SET scratch = NULL WHERE session_id = %s", (session_id,))
    return held
