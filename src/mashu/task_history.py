"""Store and read immutable task checkpoints, attempts, decisions, and artifact references."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal, get_args
from uuid import UUID

import psycopg

from mashu import events, redact, tasks
from mashu.errors import MalformedRequestError, MashuError, OverLimit

ArtifactKind = Literal[
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
]
ARTIFACT_KINDS: tuple[str, ...] = get_args(ArtifactKind)

HISTORY_LIMITS = {
    "what_changed": 500,
    "attempt": 300,
    "result": 500,
    "reason": 500,
    "next": 300,
    "decision": 300,
    "locator": 500,
    "label": 300,
}


def _what_changed_problems(what_changed: str | None) -> list[str | OverLimit]:
    if not what_changed or not what_changed.strip():
        return ["what_changed is required: history needs a short account of the work"]
    limit = HISTORY_LIMITS["what_changed"]
    if len(what_changed) > limit:
        return [OverLimit("what_changed", limit, len(what_changed))]
    return []


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
    problems = _what_changed_problems(what_changed)
    if problems:
        raise MalformedRequestError(problems)
    evidence_ids = list(evidence or [])
    _validate_evidence(cur, task_id, evidence_ids)
    tasks._gate({"what_changed": what_changed, **tasks._texts(current["state"])})
    return _insert_checkpoint(
        cur,
        task_id,
        actor=actor,
        what_changed=what_changed,
        state=current["state"],
        evidence=evidence_ids,
    )


#: Keys each checkpoint history item takes, required first.
ITEM_KEYS = {
    "attempts": (("attempt",), ("result", "reason", "next")),
    "decisions": (("decision",), ("reason", "supersedes_id")),
    "artifacts": (("kind", "locator"), ("label",)),
}


def _item(name: str, where: str, item: Any) -> tuple[dict[str, Any], list[str | OverLimit]]:
    """One history item with its keys filled in, and every problem with it.

    `where` labels the item's keys in the problems, such as `attempts[0]`; empty, each key
    is named alone.
    """
    required, optional = ITEM_KEYS[name]
    allowed = (*required, *optional)
    if not isinstance(item, dict):
        return {}, [f"{where or name} must be an object with keys {', '.join(allowed)}"]
    problems: list[str | OverLimit] = []
    unknown = sorted(set(item) - set(allowed))
    if unknown:
        problems.append(
            f"{where or name} has unknown key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(allowed)}"
        )
    missing = [
        key
        for key in required
        if item.get(key) is None or (isinstance(item[key], str) and not item[key].strip())
    ]
    if missing:
        problems.append(f"{where or name} is missing required key(s) {', '.join(missing)}")
    row = {key: item.get(key) for key in allowed}
    for key, value in row.items():
        label = f"{where}.{key}" if where else key
        if value is None or key in missing:
            continue
        if key == "supersedes_id":
            try:
                row[key] = value if isinstance(value, UUID) else UUID(str(value))
            except ValueError:
                problems.append(f"{label} must be a UUID")
        elif not isinstance(value, str):
            problems.append(f"{label} must be text")
        elif key == "kind":
            if value not in ARTIFACT_KINDS:
                problems.append(
                    f"{label} must be one of {', '.join(ARTIFACT_KINDS)} (got '{value}')"
                )
        elif len(value) > HISTORY_LIMITS[key]:
            problems.append(OverLimit(label, HISTORY_LIMITS[key], len(value)))
    return row, problems


def _items(
    name: str, items: list[Any] | None
) -> tuple[list[dict[str, Any]], list[str | OverLimit]]:
    rows, problems = [], []
    for index, item in enumerate(items or []):
        row, found = _item(name, f"{name}[{index}]", item)
        rows.append(row)
        problems += found
    return rows, problems


def _gate_item(where: str, row: dict[str, Any]) -> redact.Verdict:
    """The entrance check on an item's free text; kind and supersedes_id are not text."""
    return tasks._gate(
        {
            f"{where}.{key}" if where else key: value
            for key, value in row.items()
            if key not in ("kind", "supersedes_id")
        }
    )


def _check_supersedes(cur: psycopg.Cursor, task_id: UUID, supersedes_id: UUID | None) -> None:
    if supersedes_id is None:
        return
    cur.execute("SELECT task_id FROM decision WHERE decision_id = %s", (supersedes_id,))
    prior = cur.fetchone()
    if prior is None:
        raise MashuError(f"no decision {supersedes_id} to supersede")
    if prior["task_id"] != task_id:
        raise MashuError(f"decision {supersedes_id} belongs to another task")


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
    attempts: list[Any] | None = None,
    decisions: list[Any] | None = None,
    artifacts: list[Any] | None = None,
) -> dict[str, Any]:
    """Replace the given state fields, append the history they rest on, and freeze it all.

    Every item is checked before the first write, so a refused item leaves the state and
    history as they were. Malformed items and fields over their own ceilings are refused
    together; the card and detail budgets, which depend on the whole state, come after.
    Artifacts linked here become evidence of the checkpoint.
    """
    given = {
        "goal": goal,
        "approach": approach,
        "status_text": status_text,
        "open_questions": open_questions,
        "blockers": blockers,
        "next_actions": next_actions,
    }
    attempt_items, attempt_problems = _items("attempts", attempts)
    decision_items, decision_problems = _items("decisions", decisions)
    artifact_items, artifact_problems = _items("artifacts", artifacts)
    evidence_ids = list(evidence or [])

    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    current = tasks.task_get(cur, task_id)["state"]
    problems = [
        *_what_changed_problems(what_changed),
        *tasks.limit_problems(tasks.replaced_state(current, **given), current),
        *attempt_problems,
        *decision_problems,
        *artifact_problems,
    ]
    if problems:
        raise MalformedRequestError(problems)
    _validate_evidence(cur, task_id, evidence_ids)
    verdicts = [tasks._gate({"what_changed": what_changed})]
    for name, rows in (
        ("attempts", attempt_items),
        ("decisions", decision_items),
        ("artifacts", artifact_items),
    ):
        verdicts += [_gate_item(f"{name}[{index}]", row) for index, row in enumerate(rows)]
    for row in decision_items:
        _check_supersedes(cur, task_id, row["supersedes_id"])

    updated = tasks.task_update(
        cur, task_id, actor=actor, expect_updated_at=expect_updated_at, **given
    )
    recorded_attempts = [
        _insert_attempt(cur, task_id, actor=actor, **item) for item in attempt_items
    ]
    recorded_decisions = [
        _insert_decision(cur, task_id, actor=actor, **item) for item in decision_items
    ]
    recorded_artifacts = [
        _insert_artifact(cur, task_id, actor=actor, **item) for item in artifact_items
    ]
    frozen = _insert_checkpoint(
        cur,
        task_id,
        actor=actor,
        what_changed=what_changed,
        state=updated["state"],
        evidence=evidence_ids + [row["reference_id"] for row in recorded_artifacts],
    )
    result = {
        **updated,
        "checkpoint": frozen,
        "attempts": recorded_attempts,
        "decisions": recorded_decisions,
        "artifacts": recorded_artifacts,
    }
    for verdict in verdicts:
        report = _gate_report(verdict)
        result["unchecked"] = bool(result.get("unchecked")) or report["unchecked"]
        if report.get("malformed"):
            result["malformed"] = max(result.get("malformed", 0), report["malformed"])
    return result


def _record(
    cur: psycopg.Cursor, task_id: UUID, name: str, fields: dict[str, Any]
) -> tuple[dict[str, Any], redact.Verdict]:
    """Check one history item written on its own, as the CLI does, and gate its text."""
    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    row, problems = _item(name, "", fields)
    if problems:
        raise MalformedRequestError(problems)
    return row, _gate_item("", row)


def _insert_attempt(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    attempt: str,
    result: str | None = None,
    reason: str | None = None,
    next: str | None = None,
) -> dict[str, Any]:
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
    return row


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
    fields = {"attempt": attempt, "result": result, "reason": reason, "next": next}
    fields, verdict = _record(cur, task_id, "attempts", fields)
    row = _insert_attempt(cur, task_id, actor=actor, **fields)
    return {**row, **_gate_report(verdict)}


def _insert_decision(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    decision: str,
    reason: str | None = None,
    supersedes_id: UUID | None = None,
) -> dict[str, Any]:
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
    return row


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
    fields = {"decision": decision, "reason": reason, "supersedes_id": supersedes_id}
    fields, verdict = _record(cur, task_id, "decisions", fields)
    _check_supersedes(cur, task_id, fields["supersedes_id"])
    row = _insert_decision(cur, task_id, actor=actor, **fields)
    return {**row, **_gate_report(verdict)}


def _insert_artifact(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    kind: str,
    locator: str,
    label: str | None = None,
) -> dict[str, Any]:
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
    return row


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
    fields = {"kind": kind, "locator": locator, "label": label}
    fields, verdict = _record(cur, task_id, "artifacts", fields)
    row = _insert_artifact(cur, task_id, actor=actor, **fields)
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
