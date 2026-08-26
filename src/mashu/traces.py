"""Traces: dated observations that are not knowledge (specification 4.2).

The first time something is worked out it does not hurt, so nobody reports it.
That is why the second time cannot be proven: there is nothing to match
against. A trace is written during the work, for nothing in return, purely so
that a later derivation of the same thing has a counterpart to collide with.

Nothing here is reviewed and nothing here is pushed. A trace claims only "on
this day it looked like this", which is why thirty days is enough — what rots
is the implied claim that it is still true, and a trace never made one.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import config, events, match, redact
from mashu.errors import RefusedError

_INSERT = """
INSERT INTO trace (content, scope_id, source, created_by, expires_at)
VALUES (%s, %s, %s, %s, now() + make_interval(days => %s))
RETURNING *
"""

_REDERIVATION_NOTE = (
    "this closely repeats an existing trace: if working it out again cost you "
    "time, pain_report (kind=friction) is what turns the second derivation into "
    "evidence a rule can be admitted on"
)


def put_trace(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    scope_id: UUID | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Leave one line about something worked out, and say if it has been here before."""
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    cur.execute(_INSERT, (content, scope_id, source, actor, config.trace_ttl_days()))
    row = cur.fetchone()
    events.record(
        cur,
        "trace_recorded",
        actor,
        trace_id=row["trace_id"],
        detail={"scope_id": str(scope_id) if scope_id else None},
    )

    similar = match.similar_traces(cur, content, exclude=row["trace_id"])
    note = None
    if similar and similar[0]["score"] >= config.match_threshold():
        note = _REDERIVATION_NOTE
    return {
        "trace_id": row["trace_id"],
        "expires_at": row["expires_at"],
        "similar": similar,
        "note": note,
        "unchecked": verdict.unchecked,
    }


def search_traces(
    cur: psycopg.Cursor,
    *,
    query: str | None = None,
    scope_id: UUID | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """The only search in the system, and no knowledge comes back through it."""
    params = {"query": query, "scope": scope_id, "floor": config.SHOW_THRESHOLD, "limit": limit}
    scope_clause = (
        "AND (%(scope)s::uuid IS NULL OR t.scope_id IS NULL OR t.scope_id = %(scope)s::uuid)"
    )
    if query:
        cur.execute(
            f"""
            SELECT t.*, s.name AS scope_name, similarity(t.content, %(query)s) AS score
            FROM trace t LEFT JOIN scope s ON s.scope_id = t.scope_id
            WHERE t.expires_at > now()
              AND similarity(t.content, %(query)s) >= %(floor)s
              {scope_clause}
            ORDER BY score DESC, t.created_at DESC
            LIMIT %(limit)s
            """,
            params,
        )
    else:
        cur.execute(
            f"""
            SELECT t.*, s.name AS scope_name
            FROM trace t LEFT JOIN scope s ON s.scope_id = t.scope_id
            WHERE t.expires_at > now()
              {scope_clause}
            ORDER BY t.created_at DESC
            LIMIT %(limit)s
            """,
            params,
        )
    return cur.fetchall()


def freeze_trace(cur: psycopg.Cursor, trace: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Copy a trace into the ledger so that citing it survives its expiry.

    Evidence has to outlive the thing it was drawn from. A memory whose only
    support expired in three weeks would be an active claim with nothing
    underneath it, which is the one state section 5 forbids.
    """
    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, source, from_trace, created_by)
        VALUES ('friction', %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            f"trace frozen as evidence; first observed {trace['created_at'].date().isoformat()}",
            trace["content"],
            trace["scope_id"],
            trace.get("source"),
            trace["trace_id"],
            actor,
        ),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "trace_frozen",
        actor,
        ledger_id=row["ledger_id"],
        trace_id=trace["trace_id"],
    )
    return row
