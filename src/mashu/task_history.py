"""The history that explains how a task got where its state says it is.

Current State is replaced and delivered only while it is active. These rows
are the durable account of the work, kept append-only and pulled by request.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import psycopg

from mashu import events, tasks
from mashu.errors import MashuError, OverLimitError

ARTIFACT_KINDS = (
    "git_commit",
    "git_branch",
    "file",
    "document",
    "obsidian",
    "issue",
    "dataset",
    "log",
    "url",
    "other",
)

_HISTORY_LIMITS = {
    "what_changed": 500,
    "attempt": 300,
    "result": 500,
    "reason": 500,
    "next": 300,
    "decision": 300,
    "locator": 500,
    "label": 300,
}


def _check_text_limit(field: str, value: str | None) -> None:
    limit = _HISTORY_LIMITS[field]
    if value is not None and len(value) > limit:
        raise OverLimitError(field, limit, len(value))


def _require_text(field: str, value: str | None) -> None:
    if not value or not value.strip():
        raise MashuError(f"{field} is required: history needs a short account of the work")


def _gate_report(verdict: Any) -> dict[str, Any]:
    return tasks._gate_report(verdict)


def _validate_evidence(cur: psycopg.Cursor, task_id: UUID, evidence: list[UUID]) -> None:
    """Refuse references that are absent or belong to another task."""
    if any(item is None for item in evidence):
        raise MashuError("evidence contains a NULL element")
    if not evidence:
        return

    cur.execute(
        "SELECT reference_id, task_id FROM artifact_reference WHERE reference_id = ANY(%s)",
        (evidence,),
    )
    found = {row["reference_id"]: row["task_id"] for row in cur.fetchall()}
    missing = [reference_id for reference_id in evidence if reference_id not in found]
    if missing:
        named = ", ".join(str(reference_id) for reference_id in missing)
        raise MashuError(f"evidence names artifact rows that do not exist: {named}")

    foreign = [reference_id for reference_id in evidence if found[reference_id] != task_id]
    if foreign:
        named = ", ".join(str(reference_id) for reference_id in foreign)
        raise MashuError(f"evidence names artifact rows belonging to another task: {named}")


def _insert_checkpoint(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    what_changed: str,
    state: dict[str, Any],
    evidence: list[UUID],
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO task_checkpoint (
            task_id, what_changed, goal, approach, status_text,
            open_questions, blockers, next_actions, evidence, created_at, created_by
        )
        VALUES (%(task)s, %(what_changed)s, %(goal)s, %(approach)s, %(status_text)s,
                %(open_questions)s, %(blockers)s, %(next_actions)s, %(evidence)s,
                clock_timestamp(), %(actor)s)
        RETURNING *
        """,
        {
            "task": task_id,
            "what_changed": what_changed,
            "actor": actor,
            "evidence": evidence,
            **{field: state[field] for field in tasks.TEXT_LIMITS},
            **{field: state[field] for field in tasks.LIST_FIELDS},
        },
    )
    row = cur.fetchone()
    events.record(
        cur,
        "task_checkpointed",
        actor,
        detail={
            "task_id": str(task_id),
            "checkpoint_id": str(row["checkpoint_id"]),
            "evidence": [str(reference_id) for reference_id in evidence],
        },
    )
    return row


def _freeze_current(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    what_changed: str,
    evidence: list[UUID] | None = None,
) -> dict[str, Any]:
    """Freeze the state already present, for close's final checkpoint."""
    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    current = tasks.task_get(cur, task_id)
    _require_text("what_changed", what_changed)
    _check_text_limit("what_changed", what_changed)
    evidence_ids = list(evidence or [])
    _validate_evidence(cur, task_id, evidence_ids)
    tasks._gate(what_changed, *tasks._texts(current["state"]))
    return _insert_checkpoint(
        cur,
        task_id,
        actor=actor,
        what_changed=what_changed,
        state=current["state"],
        evidence=evidence_ids,
    )


def checkpoint(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    what_changed: str,
    expect_updated_at: dt.datetime,
    goal: str | None = None,
    approach: str | None = None,
    status_text: str | None = None,
    open_questions: list[str] | None = None,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
    evidence: list[UUID] | None = None,
) -> dict[str, Any]:
    """Replace the state and freeze that replacement as one checkpoint."""
    _require_text("what_changed", what_changed)
    _check_text_limit("what_changed", what_changed)
    evidence_ids = list(evidence or [])

    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    _validate_evidence(cur, task_id, evidence_ids)
    verdict = tasks._gate(what_changed)

    updated = tasks.task_update(
        cur,
        task_id,
        actor=actor,
        expect_updated_at=expect_updated_at,
        goal=goal,
        approach=approach,
        status_text=status_text,
        open_questions=open_questions,
        blockers=blockers,
        next_actions=next_actions,
    )
    frozen = _insert_checkpoint(
        cur,
        task_id,
        actor=actor,
        what_changed=what_changed,
        state=updated["state"],
        evidence=evidence_ids,
    )
    result = {**updated, "checkpoint": frozen}
    report = _gate_report(verdict)
    result["unchecked"] = bool(result.get("unchecked")) or report["unchecked"]
    if report.get("malformed"):
        result["malformed"] = max(result.get("malformed", 0), report["malformed"])
    return result


def attempt_record(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    attempt: str,
    result: str | None = None,
    reason: str | None = None,
    next: str | None = None,
) -> dict[str, Any]:
    """Append what was tried, what came of it, and what should happen next."""
    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    _require_text("attempt", attempt)
    for field, value in (
        ("attempt", attempt),
        ("result", result),
        ("reason", reason),
        ("next", next),
    ):
        _check_text_limit(field, value)
    verdict = tasks._gate(attempt, result, reason, next)

    cur.execute(
        """
        INSERT INTO attempt (task_id, attempt, result, reason, next, created_at, created_by)
        VALUES (%s, %s, %s, %s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (task_id, attempt, result, reason, next, actor),
    )
    row = cur.fetchone()
    tasks._renew(cur, task_id)
    events.record(
        cur,
        "attempt_recorded",
        actor,
        detail={"task_id": str(task_id), "attempt_id": str(row["attempt_id"])},
    )
    return {**row, **_gate_report(verdict)}


def decision_record(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    decision: str,
    reason: str | None = None,
    supersedes_id: UUID | None = None,
) -> dict[str, Any]:
    """Append a decision, optionally superseding one from this same task."""
    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    _require_text("decision", decision)
    _check_text_limit("decision", decision)
    _check_text_limit("reason", reason)
    verdict = tasks._gate(decision, reason)

    if supersedes_id is not None:
        cur.execute("SELECT task_id FROM decision WHERE decision_id = %s", (supersedes_id,))
        prior = cur.fetchone()
        if prior is None:
            raise MashuError(f"no decision {supersedes_id} to supersede")
        if prior["task_id"] != task_id:
            raise MashuError(f"decision {supersedes_id} belongs to another task")

    cur.execute(
        """
        INSERT INTO decision (
            task_id, decision, reason, supersedes_id, created_at, created_by
        )
        VALUES (%s, %s, %s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (task_id, decision, reason, supersedes_id, actor),
    )
    row = cur.fetchone()
    tasks._renew(cur, task_id)
    events.record(
        cur,
        "decision_recorded",
        actor,
        detail={
            "task_id": str(task_id),
            "decision_id": str(row["decision_id"]),
            "supersedes_id": str(supersedes_id) if supersedes_id is not None else None,
        },
    )
    return {**row, **_gate_report(verdict)}


def artifact_link(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    kind: str,
    locator: str,
    label: str | None = None,
) -> dict[str, Any]:
    """Append a reference to the external place where the artifact lives."""
    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    if kind not in ARTIFACT_KINDS:
        raise MashuError(
            f"unknown artifact kind '{kind}' (expected one of {', '.join(ARTIFACT_KINDS)})"
        )
    _require_text("locator", locator)
    _check_text_limit("locator", locator)
    _check_text_limit("label", label)
    verdict = tasks._gate(locator, label)

    cur.execute(
        """
        INSERT INTO artifact_reference (task_id, kind, locator, label, created_at)
        VALUES (%s, %s, %s, %s, clock_timestamp())
        RETURNING *
        """,
        (task_id, kind, locator, label),
    )
    row = cur.fetchone()
    tasks._renew(cur, task_id)
    events.record(
        cur,
        "artifact_linked",
        actor,
        detail={"task_id": str(task_id), "reference_id": str(row["reference_id"])},
    )
    return {**row, **_gate_report(verdict)}


def attempt_list(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """Return a task's attempts newest first, without reshaping the rows."""
    cur.execute(
        """
        SELECT * FROM attempt
        WHERE task_id = %s
        ORDER BY created_at DESC, attempt_id DESC
        """,
        (task_id,),
    )
    return cur.fetchall()


def decision_list(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """Return a task's decisions newest first, without reshaping the rows."""
    cur.execute(
        """
        SELECT * FROM decision
        WHERE task_id = %s
        ORDER BY created_at DESC, decision_id DESC
        """,
        (task_id,),
    )
    return cur.fetchall()


def artifact_list(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """Return a task's artifact references newest first, without reshaping rows."""
    cur.execute(
        """
        SELECT * FROM artifact_reference
        WHERE task_id = %s
        ORDER BY created_at DESC, reference_id DESC
        """,
        (task_id,),
    )
    return cur.fetchall()


def checkpoint_list(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """Return a task's frozen checkpoints newest first, without reshaping rows."""
    cur.execute(
        """
        SELECT * FROM task_checkpoint
        WHERE task_id = %s
        ORDER BY created_at DESC, checkpoint_id DESC
        """,
        (task_id,),
    )
    return cur.fetchall()


def expanded_task(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    attempts: bool = False,
    decisions: bool = False,
    artifacts: bool = False,
    checkpoints: bool = False,
) -> dict[str, Any]:
    """Return the task and only the history explicitly pulled with it."""
    expanded = tasks.task_get(cur, task_id)
    if attempts:
        expanded["attempts"] = attempt_list(cur, task_id)
    if decisions:
        expanded["decisions"] = decision_list(cur, task_id)
    if artifacts:
        expanded["artifacts"] = artifact_list(cur, task_id)
    if checkpoints:
        expanded["checkpoints"] = checkpoint_list(cur, task_id)
    return expanded
