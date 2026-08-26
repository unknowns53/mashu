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

from mashu import capacity, config, events, match, redact
from mashu.errors import MashuError, RefusedError

KINDS = ("incident", "rederivation", "user_explicit")

#: What the ledger records when an agent carries an instruction in (5.1). The
#: wording is aimed at the person reviewing it: what is on file is that an
#: agent said this was asked for, which is a different fact from its having
#: been asked for.
CLAIMED_WHAT = "a user instruction carried by an agent; confirm before it stands"

_TOMBSTONE_NOTE = (
    "a retired memory already covers this; its retire reason is the answer. "
    "No nomination was created. If the retirement itself is wrong, that is a "
    "human decision to make with the reason in view (mashu remember)."
)


def validate_evidence(cur: psycopg.Cursor, evidence: list[UUID]) -> None:
    """Refuse evidence that names nothing, in Python before the trigger does.

    The database enforces this too, and that is the enforcement that counts.
    Doing it here as well is only so a caller gets a sentence naming the
    missing ids instead of a raised plpgsql exception with a transaction
    already poisoned behind it.
    """
    if not evidence:
        raise MashuError("a nomination without evidence is a suggestion, and those are not kept")
    if any(item is None for item in evidence):
        raise MashuError("evidence contains a NULL element")
    cur.execute("SELECT ledger_id FROM ledger WHERE ledger_id = ANY(%s)", (list(evidence),))
    present = {row["ledger_id"] for row in cur.fetchall()}
    missing = [item for item in evidence if item not in present]
    if missing:
        named = ", ".join(str(item) for item in missing)
        raise MashuError(f"evidence names ledger rows that do not exist: {named}")


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
    validate_evidence(cur, list(evidence))
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


def nominate_user_explicit(
    cur: psycopg.Cursor, *, content: str, actor: str, scope_id: UUID | None = None
) -> dict[str, Any]:
    """Carry an instruction an agent says it was given, as far as the queue.

    The third admission path (5.1), and it stops here. An agent reporting that
    the user asked for something cannot distinguish that sentence from one
    printed in a document it happened to be reading, and neither can this
    layer, so the claim is filed as a claim: a 'claimed' ledger row naming the
    agent that carried it, and a candidate a person still has to confirm. The
    only route that skips the queue is somebody typing `mashu remember`.
    """
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)", (capacity.LOCK_NAMESPACE, capacity.LOCK_PAIN)
    )

    result: dict[str, Any] = {
        "ledger_id": None,
        "nomination": None,
        "nomination_existing": False,
        "tombstone_suppressed": False,
        "unchecked": verdict.unchecked,
    }
    if verdict.malformed:
        result["malformed"] = verdict.malformed

    threshold = config.match_threshold()

    # Retired knowledge outranks a relayed instruction, the same way it
    # outranks a reported pain. Letting this path through would be the way
    # round the refutation: an agent that read the withdrawn claim somewhere
    # can put it back in front of a reviewer with the reason left behind.
    tombstones = match.similar_tombstones(cur, content)
    if tombstones and tombstones[0]["score"] >= threshold:
        result["tombstone_suppressed"] = True
        result["matches"] = {"tombstones": tombstones}
        result["note"] = _TOMBSTONE_NOTE
        return result

    waiting = match.similar_pending_nominations(cur, content, limit=1)
    if waiting and waiting[0]["score"] >= threshold:
        result["nomination"] = waiting[0]
        result["nomination_existing"] = True
        return result

    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, scope_id, created_by)
        VALUES ('claimed', %s, %s, %s, %s)
        RETURNING ledger_id
        """,
        (CLAIMED_WHAT, content, scope_id, actor),
    )
    ledger_id = cur.fetchone()["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": "claimed"})

    result["ledger_id"] = ledger_id
    result["nomination"] = create_nomination(
        cur,
        content=content,
        kind="user_explicit",
        evidence=[ledger_id],
        actor=actor,
        scope_id=scope_id,
    )
    return result


def pending_nominations(
    cur: psycopg.Cursor, *, include_deferred: bool = True
) -> list[dict[str, Any]]:
    """The queue, oldest first, each candidate carrying the pains it rests on.

    The evidence is expanded here rather than left as ids because the decision
    being asked for is whether these particular pains justify a permanent
    seat, and an id answers nothing.

    A deferred candidate is still pending and is still counted as such, so the
    default returns it. Only the sitting asks for the queue without it: what
    was put off is what the reader has already looked at and decided not to
    decide, and leading with it again is how a queue stops being read.
    """
    cur.execute(
        f"""
        SELECT n.*, s.name AS scope_name
        FROM nomination n LEFT JOIN scope s ON s.scope_id = n.scope_id
        WHERE n.status = 'pending'
        {"" if include_deferred else "AND n.deferred_at IS NULL"}
        ORDER BY n.created_at, n.nomination_id
        """
    )
    rows = cur.fetchall()
    for row in rows:
        row["evidence_rows"] = _evidence_rows(cur, row["evidence"])
    return rows


def deferred_count(cur: psycopg.Cursor) -> int:
    """How many pending candidates the sitting is holding back.

    The number is worth a line on the queue screen: a reader who put three
    things off a fortnight ago and sees an empty queue has been told the wrong
    thing about their own store.
    """
    cur.execute(
        "SELECT count(*) AS n FROM nomination WHERE status = 'pending' AND deferred_at IS NOT NULL"
    )
    return cur.fetchone()["n"]


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


#: What both decision paths say when the row moved under them. The message is
#: one string because the two ways of losing the race — reading a decided row,
#: and updating a row somebody decided in between — are the same event to
#: whoever pressed the key.
NOT_PENDING = "nomination is no longer pending"


def _require_pending(cur: psycopg.Cursor, nomination_id: UUID) -> dict[str, Any]:
    """The row, locked for this transaction, or an error saying it is decided.

    FOR UPDATE because admitting and declining both read the row, act on what
    they read, and write. Two reviewers on the same nomination would otherwise
    both see 'pending': one admits, the other declines, and the queue ends up
    holding a decision nobody made in full.
    """
    cur.execute("SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE", (nomination_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no nomination {nomination_id}")
    if row["status"] != "pending":
        raise MashuError(f"{NOT_PENDING}: it was already {row['status']}")
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
        WHERE nomination_id = %s AND status = 'pending'
        """,
        (memory["memory_id"], actor, nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)

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
        WHERE nomination_id = %s AND status = 'pending'
        RETURNING *
        """,
        (reason, actor, nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_declined",
        actor,
        nomination_id=nomination_id,
        detail={"reason": reason},
    )
    return row


def defer(cur: psycopg.Cursor, nomination_id: UUID, *, actor: str, reason: str) -> dict[str, Any]:
    """Put a candidate off, on the record, without deciding it.

    The reason is required for the same cause declining needs one, and a
    sharper one: nothing here is settled, so the note is the whole of what the
    next reader inherits. "Not now" without it is a candidate that sank for no
    stated cause, which is indistinguishable from one nobody ever read.

    Deferring again overwrites, because what is wanted is the current reason
    for it still waiting, not the history of a decision that was never made.
    """
    _require_pending(cur, nomination_id)
    if not reason or not reason.strip():
        raise MashuError("putting a candidate off needs a reason: it is all the next reader gets")
    cur.execute(
        """
        UPDATE nomination
        SET deferred_at = now(), defer_reason = %s
        WHERE nomination_id = %s AND status = 'pending'
        RETURNING *
        """,
        (reason, nomination_id),
    )
    if cur.rowcount != 1:
        raise MashuError(NOT_PENDING)
    row = cur.fetchone()
    events.record(
        cur,
        "nomination_deferred",
        actor,
        nomination_id=nomination_id,
        detail={"reason": reason},
    )
    return row
