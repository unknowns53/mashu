"""Manage active memories, retirement reasons, and delivery projects."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, match, nominations, redact, settings, topics
from mashu.errors import (
    MalformedRequestError,
    MashuError,
    OverLimit,
    RefusedError,
    RetiredConflictError,
)

DELIVERIES = ("always", "project", "topic")

_EXPLICIT_WHAT = "recorded by the user's own hand"

#: The longest reason one write leaves on its evidence row, which is read but never pushed.
WHY_LIMIT = 500


def check_body(
    content: str,
    *,
    limit: int,
    why: str | None = None,
    field: str = "content",
    rest: str | None = "why",
) -> None:
    """Refuse a new body over `limit` characters, naming `rest` as where its reason goes.

    Every matching session is handed a body whole, and capacity bounds what the bodies cost
    together, not what one of them costs; settings.limit(cur, "content_limit") is the limit.
    Only text a caller writes is held to it. A body carried over unchanged was admitted under
    the limit of its day.
    """
    problems = body_problems(content, limit=limit, why=why, field=field, rest=rest)
    if problems:
        raise MalformedRequestError(problems)


def body_problems(
    content: str,
    *,
    limit: int,
    why: str | None = None,
    field: str = "content",
    rest: str | None = "why",
) -> list[str | OverLimit]:
    """What check_body refuses, for a caller reporting it with the request's other problems."""
    problems: list[str | OverLimit] = []
    if not content.strip():
        problems.append(f"{field} is empty; a Memory body states the rule itself")
    if len(content) > limit:
        where = (
            f"put its reason, measurements, dates, and history in {rest}"
            if rest
            else "leave its reason, measurements, dates, and history out of it"
        )
        advice = (
            f"a Memory body is pushed to every matching session, so keep the rule alone and {where}"
        )
        problems.append(OverLimit(field, limit, len(content), advice=advice))
    if why is not None and len(why) > WHY_LIMIT:
        problems.append(OverLimit("why", WHY_LIMIT, len(why)))
    return problems


def check_delivery(
    delivery: str,
    project_id: UUID | None,
    topic_id: UUID | None = None,
) -> None:
    """Refuse a delivery that lacks the one detail its route needs, or carries another's."""
    if delivery not in DELIVERIES:
        raise MashuError(f"unknown delivery '{delivery}' (expected one of {', '.join(DELIVERIES)})")
    if delivery == "project" and project_id is None:
        raise MashuError("delivery 'project' needs a project to be delivered to")
    if delivery == "topic" and topic_id is None:
        raise MashuError("delivery 'topic' needs the topic it is read with")
    if delivery != "topic" and topic_id is not None:
        raise MashuError("a topic only applies to delivery 'topic'")


def topic_home(cur: psycopg.Cursor, delivery: str, topic_id: UUID | None) -> UUID | None:
    """The project a topic Memory takes from its topic, which must still be open."""
    if delivery != "topic" or topic_id is None:
        return None
    return topics.lock_open_topic(cur, topic_id)["project_id"]


def delivered_project(
    cur: psycopg.Cursor, delivery: str, project_id: UUID | None, topic_id: UUID | None
) -> UUID | None:
    """The project_id a Memory row keeps for its delivery.

    An always rule reaches every session, so a project named with it (a candidate's place of
    origin, a project picked before the delivery changed) is dropped rather than kept as a home
    that would hold the project against removal.
    """
    if delivery == "always":
        return None
    if delivery == "topic":
        return topic_home(cur, delivery, topic_id)
    return project_id


def _retired_conflicts(
    cur: psycopg.Cursor, content: str, acknowledged_conflicts: list[UUID] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The retirements a direct write resembles, and the withdrawn or legacy ones among them.

    Refuses until `acknowledged_conflicts` names exactly those withdrawn or legacy rows, so
    a body that repeats a withdrawn rule is written only after its reason has been read.
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )
    tombstones = match.similar_tombstones(cur, content, limit=None)
    retirement_matches = [row for row in tombstones if row["score"] >= config.match_threshold()]
    overruled = [
        row for row in retirement_matches if row.get("retirement_kind") in ("withdrawn", "legacy")
    ]
    expected_conflicts = {row["memory_id"] for row in overruled}
    if expected_conflicts != set(acknowledged_conflicts or []):
        if expected_conflicts:
            raise RetiredConflictError(
                "this repeats retired memories marked withdrawn or legacy; read and "
                "acknowledge those retirement reasons before writing it back",
                overruled,
            )
        if acknowledged_conflicts:
            raise MashuError("no current withdrawn or legacy conflicts match the acknowledgment")
    return retirement_matches, overruled


#: The projects a Memory reaches, its home project first, for a query naming memory m and its home
#: project s. project_name joins them for listings; project_names keeps them apart for forms.
PROJECT_NAMES = """
    array_remove(ARRAY[s.name] || ARRAY(
        SELECT x.name FROM memory_project ms JOIN project x ON x.project_id = ms.project_id
        WHERE ms.memory_id = m.memory_id ORDER BY x.name
    ), NULL) AS project_names,
    array_to_string(array_remove(ARRAY[s.name] || ARRAY(
        SELECT x.name FROM memory_project ms JOIN project x ON x.project_id = ms.project_id
        WHERE ms.memory_id = m.memory_id ORDER BY x.name
    ), NULL), ', ') AS project_name
"""


def _has_other_projects(cur: psycopg.Cursor) -> bool:
    # Bootstrap runs from the SessionStart hook, so a store without the table must still answer.
    cur.execute("SELECT to_regclass('memory_project') IS NOT NULL AS present")
    return cur.fetchone()["present"]


def other_projects(cur: psycopg.Cursor, memory_id: UUID) -> list[UUID]:
    """The projects a project Memory reaches besides its home project."""
    if not _has_other_projects(cur):
        return []
    cur.execute(
        "SELECT project_id FROM memory_project WHERE memory_id = %s ORDER BY project_id",
        (memory_id,),
    )
    return [row["project_id"] for row in cur.fetchall()]


def _check_other_projects(delivery: str, home: UUID | None, others: Sequence[UUID]) -> list[UUID]:
    """The other projects once checked against the delivery and the home project."""
    others = list(dict.fromkeys(others))
    if others and delivery != "project":
        raise MashuError(f"only a project memory reaches several projects, not a {delivery} one")
    return [project_id for project_id in others if project_id != home]


def _write_other_projects(
    cur: psycopg.Cursor, memory_id: UUID, others: Sequence[UUID], *, actor: str
) -> None:
    cur.execute(
        "DELETE FROM memory_project WHERE memory_id = %s AND NOT (project_id = ANY(%s))",
        (memory_id, list(others)),
    )
    for project_id in others:
        cur.execute(
            "INSERT INTO memory_project (memory_id, project_id, created_by) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (memory_id, project_id, actor),
        )


def remember(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    project_id: UUID | None = None,
    delivery: str = "always",
    topic_id: UUID | None = None,
    acknowledged_conflicts: list[UUID] | None = None,
    why: str | None = None,
    also_project_ids: Sequence[UUID] = (),
) -> dict[str, Any]:
    """Write a rule straight into the active set, with the writing as its evidence.

    `why` becomes the evidence row's account, where the reason is kept without being pushed.
    A project rule also reaches `also_project_ids` besides `project_id`.
    """
    why = (why or "").strip() or None
    check_body(content, limit=settings.limit(cur, "content_limit"), why=why)
    check_delivery(delivery, project_id, topic_id)
    others = _check_other_projects(delivery, project_id, also_project_ids)
    project_id = delivered_project(cur, delivery, project_id, topic_id)

    verdict = redact.gate({"content": content, "why": why})

    retirement_matches, overruled = _retired_conflicts(cur, content, acknowledged_conflicts)
    warnings = [row for row in retirement_matches if row not in overruled]
    if overruled:
        approval_source = {
            "kind": "user_direct",
            "conflict_ids": sorted(str(row["memory_id"]) for row in overruled),
        }
    else:
        approval_source = {"kind": "user_direct"}
    admission = capacity.check_admission(
        cur,
        content=content,
        delivery=delivery,
        project_id=project_id,
        topic_id=topic_id,
        also_project_ids=others,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, project_id, created_by)
        VALUES ('explicit', %s, %s, %s, %s)
        RETURNING ledger_id
        """,
        (why or _EXPLICIT_WHAT, content, project_id, actor),
    )
    ledger_id = cur.fetchone()["ledger_id"]
    events.record(cur, "pain_recorded", actor, ledger_id=ledger_id, detail={"kind": "explicit"})

    nominations.validate_evidence(cur, [ledger_id])
    cur.execute(
        """
        INSERT INTO memory
            (content, project_id, delivery, topic_id, evidence, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (content, project_id, delivery, topic_id, [ledger_id], actor),
    )
    memory = cur.fetchone()
    if others:
        _write_other_projects(cur, memory["memory_id"], others, actor=actor)
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory["memory_id"], content, actor, _EXPLICIT_WHAT),
    )
    detail: dict[str, Any] = {"delivery": delivery, "approval_source": approval_source}
    if others:
        detail["also_project_ids"] = [str(project_id) for project_id in others]
    if topic_id is not None:
        detail["topic_id"] = str(topic_id)
    if overruled:
        detail["overrides"] = [str(row["memory_id"]) for row in overruled]
    if retirement_matches:
        detail["retirement_conflicts"] = [
            {
                "memory_id": str(row["memory_id"]),
                "retirement_kind": row.get("retirement_kind") or "legacy",
            }
            for row in retirement_matches
        ]
    events.record(
        cur,
        "memory_created",
        actor,
        memory_id=memory["memory_id"],
        ledger_id=ledger_id,
        detail=detail,
    )
    return {
        **memory,
        **_gate_report(verdict),
        "overrides": overruled,
        "retirement_warnings": warnings,
    }


def _gate_report(verdict: redact.Verdict) -> dict[str, Any]:
    """What the caller is told about the gate itself, beside the result."""
    report: dict[str, Any] = {"unchecked": verdict.unchecked}
    if verdict.malformed:
        report["malformed"] = verdict.malformed
    return report


def get_memory(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any] | None:
    cur.execute("SELECT * FROM memory WHERE memory_id = %s", (memory_id,))
    return cur.fetchone()


def memory_details(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any] | None:
    """Read a Memory for an explicit management or change decision.

    Its evidence and revisions come newest first, so the latest account leads.
    """
    cur.execute(
        """
        SELECT m.*, """
        + PROJECT_NAMES
        + """,
               t.name AS topic_name, t.trigger AS topic_trigger,
               r.revision_id AS current_revision_id,
               r.created_at AS revision_created_at
        FROM memory m
        LEFT JOIN project s ON s.project_id = m.project_id
        LEFT JOIN topic t ON t.topic_id = m.topic_id
        LEFT JOIN LATERAL (
            SELECT revision_id, created_at FROM memory_revision
            WHERE memory_id = m.memory_id AND content = m.content
            ORDER BY created_at DESC, revision_id DESC LIMIT 1
        ) r ON true
        WHERE m.memory_id = %s
        """,
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    row["basis"] = nominations._evidence_rows(cur, row["evidence"])
    cur.execute(
        """
        SELECT revision_id, content, actor, note, created_at
        FROM memory_revision WHERE memory_id = %s ORDER BY created_at DESC, revision_id DESC
        """,
        (memory_id,),
    )
    row["revisions"] = cur.fetchall()
    cur.execute(
        """
        SELECT event_type, actor, detail, created_at
        FROM event_log
        WHERE memory_id = %s
          AND event_type IN ('memory_retired', 'memory_restored', 'memory_retirement_classified')
        ORDER BY event_id
        """,
        (memory_id,),
    )
    row["retirement_history"] = cur.fetchall()
    row["retirement"] = None
    if row["status"] == "retired":
        row["retirement"] = {
            "kind": row["retirement_kind"] or "legacy",
            "reason": row["retire_reason"],
            "retired_at": row["retired_at"],
            "superseded_by": row["superseded_by"],
            "relocated_to_kind": row["relocated_to_kind"],
            "relocated_to_id": row["relocated_to_id"],
        }
    return row


def _require_active(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any]:
    cur.execute("SELECT * FROM memory WHERE memory_id = %s FOR UPDATE", (memory_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no memory {memory_id}")
    if row["status"] != "active":
        raise MashuError(f"memory {memory_id} is {row['status']}, and only active rows change")
    return row


def retire(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    reason: str,
    actor: str,
    retirement_kind: str,
    superseded_by: UUID | None = None,
    relocated_to_kind: str | None = None,
    relocated_to_id: UUID | None = None,
    approval_source: dict[str, Any] | None = None,
    _legacy_compat: bool = False,
) -> dict[str, Any]:
    """Withdraw a rule, leaving the reason behind as the tombstone."""
    _require_active(cur, memory_id)
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )
    if not reason or not reason.strip():
        raise MashuError("retiring needs a reason: the reason is what later readers are given")
    if retirement_kind not in ("withdrawn", "superseded", "relocated", "legacy"):
        raise MashuError("unknown retirement kind")
    if retirement_kind == "legacy" and not _legacy_compat:
        raise MashuError("legacy retirement kind is reserved for existing data and migration")
    if retirement_kind == "superseded":
        if superseded_by is None or superseded_by == memory_id:
            raise MashuError("superseded retirement needs a different successor Memory")
        cur.execute("SELECT status FROM memory WHERE memory_id = %s", (superseded_by,))
        successor = cur.fetchone()
        if successor is None:
            raise MashuError(f"no successor memory {superseded_by}")
        cur.execute(
            """
            WITH RECURSIVE successors(memory_id, superseded_by) AS (
                SELECT memory_id, superseded_by FROM memory WHERE memory_id = %s
                UNION ALL
                SELECT m.memory_id, m.superseded_by
                FROM memory m JOIN successors s ON m.memory_id = s.superseded_by
                WHERE s.superseded_by IS NOT NULL
            )
            SELECT EXISTS (SELECT 1 FROM successors WHERE memory_id = %s) AS cycle
            """,
            (superseded_by, memory_id),
        )
        if cur.fetchone()["cycle"]:
            raise MashuError("superseded_by would create a cycle")
    elif superseded_by is not None:
        raise MashuError("superseded_by is only valid for superseded retirement")
    if retirement_kind == "relocated":
        if relocated_to_kind != "temporary_context" or relocated_to_id is None:
            raise MashuError("relocated retirement needs a Temporary Context destination")
        cur.execute("SELECT 1 FROM temporary_context WHERE context_id = %s", (relocated_to_id,))
        if cur.fetchone() is None:
            raise MashuError(f"no temporary context {relocated_to_id}")
    elif relocated_to_kind is not None or relocated_to_id is not None:
        raise MashuError("relocation destination is only valid for relocated retirement")
    verdict = redact.gate({"retire_reason": reason})
    cur.execute(
        """
        UPDATE memory
        SET status = 'retired', retire_reason = %s, retired_at = now(), updated_at = now(),
            retirement_kind = %s, superseded_by = %s, relocated_to_kind = %s,
            relocated_to_id = %s
        WHERE memory_id = %s
        RETURNING *
        """,
        (reason, retirement_kind, superseded_by, relocated_to_kind, relocated_to_id, memory_id),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "memory_retired",
        actor,
        memory_id=memory_id,
        detail={
            "retirement_kind": retirement_kind,
            "reason": reason,
            "superseded_by": str(superseded_by) if superseded_by else None,
            "relocated_to_kind": relocated_to_kind,
            "relocated_to_id": str(relocated_to_id) if relocated_to_id else None,
            "approval_source": approval_source or {"kind": "user_direct"},
        },
    )
    return {**row, **_gate_report(verdict)}


def convert_to_temporary(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    days: float,
    actor: str,
    project_id: UUID | None = None,
    also_project_ids: Sequence[UUID] = (),
    override_projects: bool = False,
    content: str | None = None,
) -> dict[str, Any]:
    """Replace an active memory with an expiring condition in one transaction.

    Without `override_projects`, keeps an always or project memory's own projects and refuses a
    topic memory (whose trigger cannot carry over). With `override_projects`, writes the condition
    to the caller's chosen projects (or every session when `project_id` is None).
    """
    from mashu import temporary

    current = _require_active(cur, memory_id)
    body = current["content"] if content is None else content.strip()
    if not body:
        raise MashuError("temporary context cannot be empty")
    if override_projects:
        target_project = project_id
        target_others: Sequence[UUID] = also_project_ids
    else:
        if current["delivery"] == "topic":
            raise MashuError(
                "a topic memory cannot become temporary because its topic's trigger would be lost"
            )
        target_project = current["project_id"] if current["delivery"] == "project" else None
        target_others = other_projects(cur, memory_id) if current["delivery"] == "project" else ()
    context = temporary.put_temporary(
        cur,
        content=body,
        actor=actor,
        days=days,
        project_id=target_project,
        also_project_ids=target_others,
    )
    until = context["expires_at"].date().isoformat()
    retired = retire(
        cur,
        memory_id,
        reason=f"converted to temporary context until {until}",
        actor=actor,
        retirement_kind="relocated",
        relocated_to_kind="temporary_context",
        relocated_to_id=context["context_id"],
    )
    events.record(
        cur,
        "memory_converted_to_temporary",
        actor,
        memory_id=memory_id,
        detail={
            "context_id": str(context["context_id"]),
            "days": days,
            "delivery": current["delivery"],
            "retirement_kind": "relocated",
            "retire_reason": retired["retire_reason"],
        },
    )
    return {"memory": retired, "temporary": context}


def revise(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    content: str,
    actor: str,
    note: str | None = None,
    acknowledged_conflicts: list[UUID] | None = None,
) -> dict[str, Any]:
    """Rewrite the body, keeping the old one in the revision history.

    A new body is checked against retirements as remember checks one, since a rewrite can
    bring back a rule that was withdrawn as wrong.
    """
    current = _require_active(cur, memory_id)
    overruled: list[dict[str, Any]] = []
    if content != current["content"]:
        check_body(content, limit=settings.limit(cur, "content_limit"), rest=None)
        _, overruled = _retired_conflicts(cur, content, acknowledged_conflicts)
    elif acknowledged_conflicts:
        raise MashuError("no current withdrawn or legacy conflicts match the acknowledgment")
    redact.gate({"content": content, "note": note})
    admission = capacity.check_admission(
        cur,
        content=content,
        delivery=current["delivery"],
        project_id=current["project_id"],
        exclude_memory_id=memory_id,
        topic_id=current["topic_id"],
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        "UPDATE memory SET content = %s, updated_at = now() WHERE memory_id = %s RETURNING *",
        (content, memory_id),
    )
    row = cur.fetchone()
    cur.execute(
        "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
        (memory_id, content, actor, note),
    )
    detail: dict[str, Any] = {"note": note}
    if overruled:
        detail["overrides"] = [str(conflict["memory_id"]) for conflict in overruled]
    events.record(cur, "memory_revised", actor, memory_id=memory_id, detail=detail)
    return row


def add_reason(cur: psycopg.Cursor, memory_id: UUID, *, why: str, actor: str) -> dict[str, Any]:
    """Add a reason to a rule after the fact, as one more evidence row.

    The ledger is append-only, so a reason left out at remember is written as the same kind
    of row remember writes, holding the body as it reads now. Neither the body nor the
    delivery changes, so `updated_at` stays and proposals read against the rule stay current.
    """
    memory = get_memory(cur, memory_id)
    if memory is None:
        raise MashuError(f"no memory {memory_id}")
    why = (why or "").strip()
    if not why:
        raise MalformedRequestError(["why needs the reason to add"])
    if len(why) > WHY_LIMIT:
        raise MalformedRequestError([OverLimit("why", WHY_LIMIT, len(why))])
    verdict = redact.gate({"why": why})
    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, project_id, created_by)
        VALUES ('explicit', %s, %s, %s, %s)
        RETURNING *
        """,
        (why, memory["content"], memory["project_id"], actor),
    )
    row = cur.fetchone()
    cur.execute(
        "UPDATE memory SET evidence = array_append(evidence, %s) WHERE memory_id = %s",
        (row["ledger_id"], memory_id),
    )
    events.record(
        cur, "memory_reason_added", actor, memory_id=memory_id, ledger_id=row["ledger_id"]
    )
    return {**row, **_gate_report(verdict)}


def restore(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    reason: str,
    actor: str,
    approval_source: dict[str, Any],
    delivery: str | None = None,
    project_id: UUID | None = None,
    clear_project: bool = False,
    topic_id: UUID | None = None,
    also_project_ids: Sequence[UUID] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    """Restore a retired body through normal text, capacity, and conflict checks.

    When `delivery` is given, restores directly into that delivery, project set, or topic
    instead of keeping the retired row's earlier placement.
    """
    cur.execute("SELECT * FROM memory WHERE memory_id = %s FOR UPDATE", (memory_id,))
    current = cur.fetchone()
    if current is None or current["status"] != "retired":
        raise MashuError("restore needs a retired memory")
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_RETIREMENT),
    )
    if not reason or not reason.strip():
        raise MashuError("restore needs the reason for reversing its retirement")
    final_content = current["content"] if content is None else content.strip()
    body_changed = final_content != current["content"]
    if body_changed:
        check_body(final_content, limit=settings.limit(cur, "content_limit"), rest=None)
    verdict = redact.gate({"restored content": final_content, "restore_reason": reason})

    conflicts = [
        row
        for row in match.similar_tombstones(cur, final_content, limit=None)
        if row["memory_id"] != memory_id
        and row["score"] >= config.match_threshold()
        and row.get("retirement_kind") in ("withdrawn", "legacy")
    ]
    expected = {str(row["memory_id"]) for row in conflicts}
    acknowledged = set(approval_source.get("conflict_ids", []))
    if expected != acknowledged:
        if expected:
            raise RetiredConflictError(
                "restore conflicts changed or need explicit acknowledgment", conflicts
            )
        if acknowledged:
            raise MashuError("no current withdrawn or legacy conflicts match the acknowledgment")

    active = [
        row
        for row in match.similar_active_memories(cur, final_content)
        if row["score"] >= config.match_threshold()
    ]
    if active:
        raise MashuError("a similar active Memory already exists; resolve that Memory first")

    before_others = other_projects(cur, memory_id)
    if delivery is not None:
        if clear_project and project_id is not None:
            raise MashuError("pass a project or clear it, not both")
        target_delivery = delivery
        target_topic = topic_id
        raw_home = (
            None if clear_project else (current["project_id"] if project_id is None else project_id)
        )
        check_delivery(target_delivery, raw_home, target_topic)
        home = delivered_project(cur, target_delivery, raw_home, target_topic)
        if also_project_ids is None:
            kept = target_delivery == "project" and current["delivery"] == "project"
            others = before_others if kept and home == current["project_id"] else []
        else:
            others = _check_other_projects(target_delivery, home, also_project_ids)
    else:
        target_delivery = current["delivery"]
        target_topic = current["topic_id"]
        home = delivered_project(cur, target_delivery, current["project_id"], target_topic)
        others = before_others if target_delivery == "project" else []

    admission = capacity.check_admission(
        cur,
        content=final_content,
        delivery=target_delivery,
        project_id=home,
        topic_id=target_topic,
        also_project_ids=others,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])
    cur.execute(
        """
        UPDATE memory
        SET content = %s, status = 'active', retire_reason = NULL, retired_at = NULL,
            updated_at = now(), retirement_kind = NULL, superseded_by = NULL,
            relocated_to_kind = NULL, relocated_to_id = NULL,
            delivery = %s, project_id = %s, topic_id = %s
        WHERE memory_id = %s AND status = 'retired'
        RETURNING *
        """,
        (final_content, target_delivery, home, target_topic, memory_id),
    )
    row = cur.fetchone()
    if body_changed:
        cur.execute(
            "INSERT INTO memory_revision (memory_id, content, actor, note) VALUES (%s, %s, %s, %s)",
            (memory_id, final_content, actor, "restored with revision"),
        )
    if before_others or others:
        _write_other_projects(cur, memory_id, others, actor=actor)
    events.record(
        cur,
        "memory_restored",
        actor,
        memory_id=memory_id,
        detail={
            "reason": reason,
            "delivery": target_delivery,
            "former_retirement_kind": current["retirement_kind"],
            "former_retire_reason": current["retire_reason"],
            "former_superseded_by": str(current["superseded_by"])
            if current["superseded_by"]
            else None,
            "former_relocated_to_kind": current["relocated_to_kind"],
            "former_relocated_to_id": str(current["relocated_to_id"])
            if current["relocated_to_id"]
            else None,
            "approval_source": approval_source,
        },
    )
    return {**row, **_gate_report(verdict)}


def set_delivery(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    delivery: str,
    actor: str,
    project_id: UUID | None = None,
    clear_project: bool = False,
    topic_id: UUID | None = None,
    approval_source: dict[str, Any] | None = None,
    also_project_ids: Sequence[UUID] | None = None,
) -> dict[str, Any]:
    """Move a rule between the opening, the openings of some projects, and a topic.

    `also_project_ids` names every project a project rule reaches besides its home project. Left
    out, a rule that stays in the same home project keeps the others it had, and a moved rule
    drops them, as the store does for any move.
    """
    current = _require_active(cur, memory_id)
    if clear_project and project_id is not None:
        raise MashuError("pass a project or clear it, not both")
    home = None if clear_project else (current["project_id"] if project_id is None else project_id)
    check_delivery(delivery, home, topic_id)
    home = delivered_project(cur, delivery, home, topic_id)
    before = other_projects(cur, memory_id)
    if also_project_ids is None:
        kept = delivery == "project" and current["delivery"] == "project"
        others = before if kept and home == current["project_id"] else []
    else:
        others = _check_other_projects(delivery, home, also_project_ids)

    admission = capacity.check_admission(
        cur,
        content=current["content"],
        delivery=delivery,
        project_id=home,
        exclude_memory_id=memory_id,
        topic_id=topic_id,
        also_project_ids=others,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        UPDATE memory
        SET delivery = %s, project_id = %s, topic_id = %s, updated_at = now()
        WHERE memory_id = %s
        RETURNING *
        """,
        (delivery, home, topic_id, memory_id),
    )
    row = cur.fetchone()
    if before or others:
        _write_other_projects(cur, memory_id, others, actor=actor)
    detail: dict[str, Any] = {"from": current["delivery"], "to": delivery}
    if before or others:
        detail["from_also_project_ids"] = sorted(str(project_id) for project_id in before)
        detail["to_also_project_ids"] = sorted(str(project_id) for project_id in others)
    if current["topic_id"] is not None or topic_id is not None:
        detail["from_topic_id"] = str(current["topic_id"]) if current["topic_id"] else None
        detail["to_topic_id"] = str(topic_id) if topic_id else None
    if approval_source is not None:
        detail["approval_source"] = approval_source
    events.record(cur, "delivery_changed", actor, memory_id=memory_id, detail=detail)
    return row


def always_memories(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT * FROM memory WHERE status = 'active' AND delivery = 'always'
        ORDER BY created_at, memory_id
        """
    )
    return cur.fetchall()


def _in_project(cur: psycopg.Cursor) -> str:
    """The condition that a memory row reaches the project %(project)s, as home or as another."""
    if not _has_other_projects(cur):
        return "project_id = %(project)s"
    return (
        "(project_id = %(project)s OR memory_id IN "
        "(SELECT memory_id FROM memory_project WHERE project_id = %(project)s))"
    )


def project_push_memories(cur: psycopg.Cursor, project_id: UUID) -> list[dict[str, Any]]:
    """The project rules a session in this project receives, whether it is their home or not."""
    cur.execute(
        f"""
        SELECT * FROM memory
        WHERE status = 'active' AND delivery = 'project' AND {_in_project(cur)}
        ORDER BY created_at, memory_id
        """,
        {"project": project_id},
    )
    return cur.fetchall()


def active_memories(cur: psycopg.Cursor, *, project_id: UUID) -> list[dict[str, Any]]:
    """Everything alive in one project, whatever route it takes to a session."""
    cur.execute(
        f"""
        SELECT * FROM memory
        WHERE status = 'active' AND {_in_project(cur)}
        ORDER BY delivery, created_at, memory_id
        """,
        {"project": project_id},
    )
    return cur.fetchall()
