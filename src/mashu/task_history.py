"""Store and read immutable task checkpoints, attempts, decisions, and artifact references."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

import psycopg

from mashu import approvals, events, redact, settings, tasks
from mashu.errors import MalformedRequestError, MashuError, OverLimit, OverLimitError
from mashu.tokens import pushed_cost

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

#: How many items of each kind one checkpoint may carry. A session that records every check
#: and measurement as an attempt or decision buries the few whose loss would cost a later
#: session, and an instruction to be selective does not hold. Artifacts are references only.
CHECKPOINT_ITEM_MAX = {"attempts": 2, "decisions": 2}

#: How many decisions a task may hold in force. Past it, a new decision must supersede as many
#: as it adds, so the agent folds settled points together instead of piling them up. The cap
#: counts what a write would leave, so decisions written before it are not refused.
DECISIONS_IN_FORCE_MAX = 15


def _what_changed_problems(what_changed: str | None) -> list[str | OverLimit]:
    limit = HISTORY_LIMITS["what_changed"]
    if what_changed and len(what_changed) > limit:
        return [OverLimit("what_changed", limit, len(what_changed))]
    return []


def _gate_report(verdict: Any) -> dict[str, Any]:
    return tasks._gate_report(verdict)


def _freeze_current(
    cur: psycopg.Cursor, task_id: UUID, *, actor: str, what_changed: str
) -> dict[str, Any]:
    """Freeze the state already present, for close's final checkpoint."""
    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    state = tasks.task_get(cur, task_id)["state"]
    problems = _what_changed_problems(what_changed)
    if problems:
        raise MalformedRequestError(problems)
    tasks._gate({"what_changed": what_changed, **tasks._texts(state)})
    cur.execute(
        """
        INSERT INTO task_checkpoint (
            task_id, what_changed, goal, approach, status_text,
            open_questions, blockers, next_actions, created_at, created_by
        )
        VALUES (%(task)s, %(what_changed)s, %(goal)s, %(approach)s, %(status_text)s,
                %(open_questions)s, %(blockers)s, %(next_actions)s,
                clock_timestamp(), %(actor)s)
        RETURNING *
        """,
        {
            "task": task_id,
            "what_changed": what_changed,
            "actor": actor,
            **{field: state[field] for field in tasks.TEXT_LIMITS},
            **{field: state[field] for field in tasks.LIST_FIELDS},
        },
    )
    row = cur.fetchone()
    events.record(
        cur,
        "task_checkpointed",
        actor,
        detail={"task_id": str(task_id), "checkpoint_id": str(row["checkpoint_id"])},
    )
    return row


#: Keys each checkpoint history item takes, required first.
ITEM_KEYS = {
    "attempts": (("attempt",), ("result", "reason", "next")),
    "decisions": (("decision",), ("reason", "supersedes_ids")),
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
        if key == "supersedes_ids":
            row[key], found = _decision_ids(label, value)
            problems += found
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


def _decision_ids(label: str, value: Any) -> tuple[list[UUID], list[str | OverLimit]]:
    """The distinct decision ids a list names, in the order given."""
    if not isinstance(value, list):
        return [], [f"{label} must be a list of decision ids"]
    ids: list[UUID] = []
    problems: list[str | OverLimit] = []
    for index, entry in enumerate(value):
        try:
            ident = entry if isinstance(entry, UUID) else UUID(str(entry))
        except ValueError:
            problems.append(f"{label}[{index}] must be a UUID")
            continue
        if ident not in ids:
            ids.append(ident)
    return ids, problems


def _items(
    name: str, items: list[Any] | None
) -> tuple[list[dict[str, Any]], list[str | OverLimit]]:
    rows: list[dict[str, Any]] = []
    problems: list[str | OverLimit] = []
    most = CHECKPOINT_ITEM_MAX.get(name)
    if most is not None and len(items or []) > most:
        problems.append(
            OverLimit(
                name,
                most,
                len(items or []),
                unit="items",
                advice="keep the ones a later session would pay to lose; checks and "
                "measurements belong in a commit or the project's documents",
            )
        )
    for index, item in enumerate(items or []):
        row, found = _item(name, f"{name}[{index}]", item)
        if name == "artifacts" and isinstance(row.get("locator"), str):
            # A locator points at a file, so its home directory is kept from ~ instead of
            # being refused.
            row["locator"] = approvals.home_relative(row["locator"])
        rows.append(row)
        problems += found
    return rows, problems


def _gate_item(where: str, row: dict[str, Any]) -> redact.Verdict:
    """The entrance check on an item's free text; kind and supersedes_ids are not text."""
    return tasks._gate(
        {
            f"{where}.{key}" if where else key: value
            for key, value in row.items()
            if key not in ("kind", "supersedes_ids")
        }
    )


def _check_supersedes(cur: psycopg.Cursor, task_id: UUID, supersedes_ids: list[UUID]) -> None:
    for supersedes_id in supersedes_ids:
        cur.execute("SELECT task_id FROM decision WHERE decision_id = %s", (supersedes_id,))
        prior = cur.fetchone()
        if prior is None:
            raise MashuError(f"no decision {supersedes_id} to supersede")
        if prior["task_id"] != task_id:
            raise MashuError(f"decision {supersedes_id} belongs to another task")


_IN_FORCE = """
SELECT d.decision_id FROM decision d
WHERE d.task_id = %s
  AND NOT EXISTS (SELECT 1 FROM decision_supersession s WHERE s.supersedes_id = d.decision_id)
"""


def _check_in_force(cur: psycopg.Cursor, task_id: UUID, items: list[dict[str, Any]]) -> None:
    """Refuse decisions that would leave the task over its cap and above where it stands.

    A write that keeps or lowers the count passes even over the cap, since a task already
    past it can only come back down by such writes.
    """
    if not items:
        return
    cur.execute(_IN_FORCE, (task_id,))
    in_force = {row["decision_id"] for row in cur.fetchall()}
    retired = {ident for item in items for ident in item["supersedes_ids"] or []} & in_force
    after = len(in_force) - len(retired) + len(items)
    if after <= DECISIONS_IN_FORCE_MAX or after <= len(in_force):
        return
    raise OverLimitError(
        "decisions in force",
        DECISIONS_IN_FORCE_MAX,
        after,
        unit="decisions",
        advice=(
            f"the task holds {len(in_force)}; let each new decision list in `supersedes_ids` "
            "the decisions in force it settles or replaces, folding several into one where "
            "they make one point, so the write adds no more than it supersedes. "
            "task_get with decisions=true lists them with their ids"
        ),
    )


def _last_own_write(cur: psycopg.Cursor, task_id: UUID, session: UUID | None, minutes: int) -> Any:
    """When this session last wrote this task's state inside the checkpoint interval."""
    if session is None or minutes <= 0:
        return None
    cur.execute(
        """
        SELECT max(created_at) AS written_at FROM event_log
        WHERE event_type = 'task_state_replaced'
          AND detail->>'task_id' = %s AND detail->>'session' = %s
          AND created_at > clock_timestamp() - make_interval(mins => %s)
        """,
        (str(task_id), str(session), minutes),
    )
    return cur.fetchone()["written_at"]


def _interval_warning(cur: psycopg.Cursor, task_id: UUID, session: UUID | None) -> str | None:
    """The warning for one session's second write to a task inside the interval.

    Agents measuring or iterating change status and next steps every few minutes and read
    each change as a reason to write, so the write is answered with a warning. It still goes
    through whatever it carries: a refusal dropped failed tries found after the first write,
    and an exemption for some content would be met by making that content up.
    """
    minutes = settings.checkpoint_interval_minutes(cur)
    written_at = _last_own_write(cur, task_id, session, minutes)
    if written_at is None:
        return None
    return (
        f"this session also wrote this task's state at {written_at:%H:%M}, inside the "
        f"{minutes}-minute interval. This write was kept. A checkpoint carries only failed "
        "tries, costly decisions, sources, and the state before you stop; progress and "
        "measurements go in a commit or the project's documents, not into another write"
    )


def checkpoint(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    expect_updated_at: dt.datetime,
    what_changed: str | None = None,
    name: str | None = None,
    goal: str | None = None,
    approach: str | None = None,
    status_text: str | None = None,
    open_questions: list[str] | None = None,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
    attempts: list[Any] | None = None,
    decisions: list[Any] | None = None,
    artifacts: list[Any] | None = None,
    session: UUID | None = None,
) -> dict[str, Any]:
    """Replace the given state fields and name, and append the history they rest on.

    Every item is checked before the first write, so a refused item leaves the state and
    history as they were. Malformed items and fields over their own ceilings are refused
    together; the card and detail budgets, which depend on the whole state, come after.
    No checkpoint row is frozen here: `what_changed` and the ids of the rows appended are
    kept on the state-replaced event, and only close freezes a state.
    """
    given = {
        "goal": goal,
        "approach": approach,
        "status_text": status_text,
        "open_questions": open_questions,
        "blockers": blockers,
        "next_actions": next_actions,
    }
    what_changed = (what_changed or "").strip() or None
    attempt_items, attempt_problems = _items("attempts", attempts)
    decision_items, decision_problems = _items("decisions", decisions)
    artifact_items, artifact_problems = _items("artifacts", artifacts)

    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    current = tasks.task_get(cur, task_id)["state"]
    warning = _interval_warning(cur, task_id, session)
    problems = [
        *_what_changed_problems(what_changed),
        *tasks.limit_problems(tasks.replaced_state(current, **given), current),
        *attempt_problems,
        *decision_problems,
        *artifact_problems,
    ]
    if problems:
        raise MalformedRequestError(problems)
    verdicts = [tasks._gate({"what_changed": what_changed})]
    for item_name, rows in (
        ("attempts", attempt_items),
        ("decisions", decision_items),
        ("artifacts", artifact_items),
    ):
        verdicts += [_gate_item(f"{item_name}[{index}]", row) for index, row in enumerate(rows)]
    for row in decision_items:
        _check_supersedes(cur, task_id, row["supersedes_ids"] or [])
    _check_in_force(cur, task_id, decision_items)

    # Ids are drawn before the state write so its event can name the rows, while every
    # refusal the state write can raise still comes before the first row is written.
    attempt_ids = [uuid4() for _ in attempt_items]
    decision_ids = [uuid4() for _ in decision_items]
    reference_ids = [uuid4() for _ in artifact_items]
    history = {
        "what_changed": what_changed,
        "attempt_ids": [str(ident) for ident in attempt_ids],
        "decision_ids": [str(ident) for ident in decision_ids],
        "reference_ids": [str(ident) for ident in reference_ids],
    }
    updated = tasks.task_update(
        cur,
        task_id,
        actor=actor,
        expect_updated_at=expect_updated_at,
        name=name,
        session=session,
        history={key: value for key, value in history.items() if value},
        **given,
    )
    result = {
        **updated,
        "attempts": [
            _insert_attempt(cur, task_id, actor=actor, attempt_id=ident, **item)
            for ident, item in zip(attempt_ids, attempt_items, strict=True)
        ],
        "decisions": [
            _insert_decision(cur, task_id, actor=actor, decision_id=ident, **item)
            for ident, item in zip(decision_ids, decision_items, strict=True)
        ],
        "artifacts": [
            _insert_artifact(cur, task_id, actor=actor, reference_id=ident, **item)
            for ident, item in zip(reference_ids, artifact_items, strict=True)
        ],
    }
    for verdict in verdicts:
        report = _gate_report(verdict)
        result["unchecked"] = bool(result.get("unchecked")) or report["unchecked"]
        if report.get("malformed"):
            result["malformed"] = max(result.get("malformed", 0), report["malformed"])
    if warning:
        result["warning"] = warning
    return result


def _insert_attempt(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    attempt_id: UUID,
    attempt: str,
    result: str | None = None,
    reason: str | None = None,
    next: str | None = None,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO attempt (
            attempt_id, task_id, attempt, result, reason, next, created_at, created_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (attempt_id, task_id, attempt, result, reason, next, actor),
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


def _insert_decision(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    decision_id: UUID,
    decision: str,
    reason: str | None = None,
    supersedes_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    supersedes_ids = supersedes_ids or []
    cur.execute(
        """
        INSERT INTO decision (decision_id, task_id, decision, reason, created_at, created_by)
        VALUES (%s, %s, %s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (decision_id, task_id, decision, reason, actor),
    )
    row = {**cur.fetchone(), "supersedes_ids": supersedes_ids}
    for supersedes_id in supersedes_ids:
        cur.execute(
            "INSERT INTO decision_supersession (decision_id, supersedes_id) VALUES (%s, %s)",
            (decision_id, supersedes_id),
        )
    tasks._renew(cur, task_id)
    events.record(
        cur,
        "decision_recorded",
        actor,
        detail={
            "task_id": str(task_id),
            "decision_id": str(row["decision_id"]),
            "supersedes_ids": [str(ident) for ident in supersedes_ids],
        },
    )
    return row


def _insert_artifact(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    reference_id: UUID,
    kind: str,
    locator: str,
    label: str | None = None,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO artifact_reference (reference_id, task_id, kind, locator, label, created_at)
        VALUES (%s, %s, %s, %s, %s, clock_timestamp())
        RETURNING *
        """,
        (reference_id, task_id, kind, locator, label),
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


def decisions_in_force(cur: psycopg.Cursor, task_id: UUID) -> dict[str, Any]:
    """The decisions no later one superseded, newest first, as far as their share holds.

    They come with every task_get because they are what an earlier session settled, and a
    session that never asks for history would otherwise propose them again. Reasons stay in
    the full history, which `decisions=True` returns.
    """
    cur.execute(
        """
        SELECT d.decision_id, d.decision, d.created_at FROM decision d
        WHERE d.task_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM decision_supersession s WHERE s.supersedes_id = d.decision_id
          )
        ORDER BY d.created_at DESC, d.decision_id DESC
        """,
        (task_id,),
    )
    rows = cur.fetchall()
    shown: list[dict[str, Any]] = []
    budget = settings.limit(cur, "decision_capacity")
    for row in rows:
        line = {
            "decision_id": row["decision_id"],
            "on": row["created_at"].date(),
            "decision": row["decision"],
        }
        if pushed_cost([item["decision"] for item in [*shown, line]]) > budget:
            break
        shown.append(line)
    return {"decisions_in_force": shown, "decisions_in_force_more": len(rows) - len(shown)}


def decision_count(cur: psycopg.Cursor, task_ids: list[UUID]) -> dict[UUID, int]:
    """How many decisions are in force on each task, for the card's details line."""
    if not task_ids:
        return {}
    cur.execute(
        """
        SELECT d.task_id, count(*) AS n FROM decision d
        WHERE d.task_id = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM decision_supersession s WHERE s.supersedes_id = d.decision_id
          )
        GROUP BY d.task_id
        """,
        (task_ids,),
    )
    return {row["task_id"]: row["n"] for row in cur.fetchall()}


def decision_list(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """Return a task's decisions newest first, each with the decisions it superseded."""
    cur.execute(
        """
        SELECT d.*,
               ARRAY(
                   SELECT s.supersedes_id FROM decision_supersession s
                   WHERE s.decision_id = d.decision_id ORDER BY s.supersedes_id
               ) AS supersedes_ids
        FROM decision d
        WHERE d.task_id = %s
        ORDER BY d.created_at DESC, d.decision_id DESC
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
    """Return the task, its decisions in force, and only the history explicitly pulled with it."""
    expanded = {**tasks.task_get(cur, task_id), **decisions_in_force(cur, task_id)}
    if attempts:
        expanded["attempts"] = attempt_list(cur, task_id)
    if decisions:
        expanded["decisions"] = decision_list(cur, task_id)
    if artifacts:
        expanded["artifacts"] = artifact_list(cur, task_id)
    if checkpoints:
        expanded["checkpoints"] = checkpoint_list(cur, task_id)
    return expanded
