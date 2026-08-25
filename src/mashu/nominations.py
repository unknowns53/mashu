"""Candidates waiting on a person (specification 5.1).

This table is the trust boundary. Nothing an agent writes reaches another
session without a human key-press, and this is where it waits for one. The
reason is not distrust of the reporting agent: an agent relaying "the user
said to record this" cannot distinguish the user's sentence from an
instruction pasted into a document it was reading, and neither can the store.
The only path that skips this queue is a person typing at their own terminal.

Because the queue is a few items a week, each one can be decided on its own
merits with its evidence next to it. v1's batch machinery existed to survive a
hundred a day, and it went out with the volume that required it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, events, redact
from mashu.errors import MashuError, RefusedError

KINDS = ("incident", "rederivation", "user_explicit")


def create_nomination(
    cur: psycopg.Cursor,
    *,
    content: str,
    kind: str,
    evidence: list[UUID],
    actor: str,
    scope_id: UUID | None = None,
) -> dict[str, Any]:
    """File a candidate. The evidence array is why it is allowed to be one."""
    if kind not in KINDS:
        raise MashuError(f"unknown nomination kind '{kind}' (expected one of {', '.join(KINDS)})")
    if not evidence:
        raise MashuError("a nomination without evidence is a suggestion, and those are not kept")
    cur.execute(
        """
        INSERT INTO nomination (content, scope_id, kind, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING *
        """,
        (content, scope_id, kind, list(evidence), actor),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_created",
        actor,
        nomination_id=row["nomination_id"],
        detail={"kind": kind, "evidence": [str(e) for e in evidence]},
    )
    return row


def pending_nominations(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """The queue, oldest first, each candidate carrying the pains it rests on.

    The evidence is expanded here rather than left as ids because the decision
    being asked for is whether these particular pains justify a permanent
    seat, and an id answers nothing.
    """
    cur.execute(
        """
        SELECT n.*, s.name AS scope_name
        FROM nomination n LEFT JOIN scope s ON s.scope_id = n.scope_id
        WHERE n.status = 'pending'
        ORDER BY n.created_at, n.nomination_id
        """
    )
    rows = cur.fetchall()
    for row in rows:
        row["evidence_rows"] = _evidence_rows(cur, row["evidence"])
    return rows


def _evidence_rows(cur: psycopg.Cursor, evidence: list[UUID]) -> list[dict[str, Any]]:
    if not evidence:
        return []
    cur.execute(
        """
        SELECT ledger_id, kind, what, prevention, created_at
        FROM ledger WHERE ledger_id = ANY(%s)
        """,
        (list(evidence),),
    )
    by_id = {row["ledger_id"]: row for row in cur.fetchall()}
    return [by_id[e] for e in evidence if e in by_id]


def _require_pending(cur: psycopg.Cursor, nomination_id: UUID) -> dict[str, Any]:
    cur.execute("SELECT * FROM nomination WHERE nomination_id = %s", (nomination_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no nomination {nomination_id}")
    if row["status"] != "pending":
        raise MashuError(f"nomination {nomination_id} was already {row['status']}")
    return row


def admit(
    cur: psycopg.Cursor,
    nomination_id: UUID,
    *,
    actor: str,
    delivery: str,
    scope_id: UUID | None = None,
    guard_action: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Turn a candidate into a memory, carrying its evidence across.

    The evidence moves with it rather than being re-derived, because what the
    person approved was this rule supported by these pains, and a memory that
    quietly points somewhere else is a different decision.
    """
    nomination = _require_pending(cur, nomination_id)
    final = nomination["content"] if content is None else content
    home = nomination["scope_id"] if scope_id is None else scope_id

    if delivery not in ("always", "scope", "guard"):
        raise MashuError(f"unknown delivery '{delivery}'")
    if delivery == "scope" and home is None:
        raise MashuError("delivery 'scope' needs a scope to be delivered to")
    if delivery == "guard":
        if not guard_action:
            raise MashuError("delivery 'guard' needs the action it stands in front of")
    else:
        guard_action = None

    verdict = redact.check(final)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    admission = capacity.check_admission(cur, content=final, delivery=delivery, scope_id=home)
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        INSERT INTO memory (content, scope_id, delivery, guard_action, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (final, home, delivery, guard_action, list(nomination["evidence"]), actor),
    )
    memory = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory["memory_id"], final, actor, f"admitted from nomination {nomination_id}"),
    )
    cur.execute(
        """
        UPDATE nomination
        SET status = 'admitted', memory_id = %s, decided_by = %s, decided_at = now()
        WHERE nomination_id = %s
        """,
        (memory["memory_id"], actor, nomination_id),
    )

    events.record(
        cur,
        "memory_created",
        actor,
        memory_id=memory["memory_id"],
        nomination_id=nomination_id,
        detail={"delivery": delivery},
    )
    events.record(
        cur,
        "nomination_admitted",
        actor,
        memory_id=memory["memory_id"],
        nomination_id=nomination_id,
    )
    return memory


def decline(cur: psycopg.Cursor, nomination_id: UUID, *, actor: str, reason: str) -> dict[str, Any]:
    """Turn a candidate down, on the record.

    The reason is required because the same pain will come back, and the next
    person to see it deserves to know this was considered and rejected rather
    than never noticed.
    """
    _require_pending(cur, nomination_id)
    if not reason or not reason.strip():
        raise MashuError("declining needs a reason: the next report of this pain will read it")
    cur.execute(
        """
        UPDATE nomination
        SET status = 'declined', decision_reason = %s, decided_by = %s, decided_at = now()
        WHERE nomination_id = %s
        RETURNING *
        """,
        (reason, actor, nomination_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_declined",
        actor,
        nomination_id=nomination_id,
        detail={"reason": reason},
    )
    return row
