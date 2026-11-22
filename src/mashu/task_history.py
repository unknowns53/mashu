"""Store and read a task's history: the user's rulings, artifact references, the checkpoint frozen
at close, and the attempts and decisions kept as a read-only record."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

import psycopg

from mashu import approvals, events, projects, redact, settings, tasks
from mashu.errors import (
    MalformedRequestError,
    MashuError,
    OverLimit,
    OverLimitError,
    RefusedError,
)

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

#: Character ceilings on history text. A ruling and the user's words beside it are held to the
#: user's settings (`ruling_chars`, `ruling_words_chars`), read at each write.
HISTORY_LIMITS = {
    "what_changed": 500,
    "locator": 500,
    "label": 300,
    "settled_in": 500,
    "note": 300,
}

#: The setting that bounds each key whose ceiling the user sets.
SETTING_LIMITS = {"ruling": "ruling_chars", "user_instruction": "ruling_words_chars"}

#: How many rulings, withdrawals, and settlements one checkpoint may carry. Each ruling or
#: withdrawal comes from something the user said in the session; settlements may close out
#: several codified rulings at once after an implementation lands.
CHECKPOINT_ITEM_MAX = {"rulings": 3, "withdraw_rulings": 3, "settle_rulings": 5}


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
    "rulings": (("ruling", "user_instruction"), ("supersedes",)),
    "withdraw_rulings": (("ruling_id", "user_instruction"), ()),
    "settle_rulings": (("ruling_id", "settled_in"), ("note",)),
    "artifacts": (("kind", "locator"), ("label",)),
}

#: Where what agents used to write as attempts and decisions now goes, for the refusal of a
#: write that still carries them.
RETIRED_ITEMS = (
    "attempts and agent decisions are no longer kept: put the approach you chose in "
    "`approach`, findings, measurements, and rejected options in the project's documents "
    "linked as `artifacts`, and what the user decided in `rulings` with the user's words"
)


def _item(name: str, where: str, item: Any) -> tuple[dict[str, Any], list[str | OverLimit]]:
    """One history item with its keys filled in, and every problem with it.

    `where` labels the item's keys in the problems, such as `rulings[0]`. A ruling's own
    length is held to the user's setting by the caller, which reads it from the store.
    """
    required, optional = ITEM_KEYS[name]
    allowed = (*required, *optional)
    if not isinstance(item, dict):
        return {}, [f"{where} must be an object with keys {', '.join(allowed)}"]
    problems: list[str | OverLimit] = []
    unknown = sorted(set(item) - set(allowed))
    if unknown:
        problems.append(
            f"{where} has unknown key(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}"
        )
    missing = [
        key
        for key in required
        if item.get(key) is None or (isinstance(item[key], str) and not item[key].strip())
    ]
    if missing:
        problems.append(f"{where} is missing required key(s) {', '.join(missing)}")
    row = {key: item.get(key) for key in allowed}
    for key, value in row.items():
        label = f"{where}.{key}"
        if value is None or key in missing:
            continue
        if key == "ruling_id":
            try:
                row[key] = value if isinstance(value, UUID) else UUID(str(value))
            except ValueError:
                problems.append(f"{label} must be a UUID")
        elif key == "supersedes":
            if not isinstance(value, list):
                problems.append(f"{label} must be a list of UUIDs")
            else:
                parsed: list[UUID] = []
                for idx, target in enumerate(value):
                    try:
                        parsed.append(target if isinstance(target, UUID) else UUID(str(target)))
                    except ValueError:
                        problems.append(f"{label}[{idx}] must be a UUID")
                row[key] = parsed
        elif not isinstance(value, str):
            problems.append(f"{label} must be text")
        elif key == "kind":
            if value not in ARTIFACT_KINDS:
                problems.append(
                    f"{label} must be one of {', '.join(ARTIFACT_KINDS)} (got '{value}')"
                )
        else:
            if key in ("ruling", "user_instruction", "settled_in", "note"):
                value = value.strip()
                row[key] = value or None if key == "note" else value
            if key in HISTORY_LIMITS and value and len(value) > HISTORY_LIMITS[key]:
                problems.append(OverLimit(label, HISTORY_LIMITS[key], len(value)))
    return row, problems


def _items(
    name: str, items: list[Any] | None, *, set_limits: dict[str, int]
) -> tuple[list[dict[str, Any]], list[str | OverLimit]]:
    """The items with their keys filled in, and every problem with them; `set_limits` holds the
    ceilings the user set, keyed as the items take them."""
    rows: list[dict[str, Any]] = []
    problems: list[str | OverLimit] = []
    most = CHECKPOINT_ITEM_MAX.get(name)
    if most is not None and len(items or []) > most:
        advice = (
            "settle only rulings whose outcome is now recorded in code, docs, a skill, or memory"
            if name == "settle_rulings"
            else "write only what the user said in this session, one point per item"
        )
        problems.append(
            OverLimit(
                name,
                most,
                len(items or []),
                unit="items",
                advice=advice,
            )
        )
    for index, item in enumerate(items or []):
        where = f"{name}[{index}]"
        row, found = _item(name, where, item)
        for key, limit in set_limits.items():
            value = row.get(key)
            if isinstance(value, str) and len(value) > limit:
                found.append(OverLimit(f"{where}.{key}", limit, len(value)))
        if name == "artifacts" and isinstance(row.get("locator"), str):
            # A locator points at a file, so its home directory is kept from ~ instead of
            # being refused.
            row["locator"] = approvals.home_relative(row["locator"])
        if name == "settle_rulings" and isinstance(row.get("settled_in"), str):
            row["settled_in"] = approvals.home_relative(row["settled_in"])
        rows.append(row)
        problems += found
    return rows, problems


def _gate_item(where: str, row: dict[str, Any]) -> redact.Verdict:
    """The entrance check on an item's free text; kind, ruling_id, and supersedes are not text."""
    return tasks._gate(
        {
            f"{where}.{key}" if where else key: value
            for key, value in row.items()
            if key not in ("kind", "ruling_id", "supersedes")
        }
    )


def _check_ruling_endings(
    cur: psycopg.Cursor,
    task_id: UUID,
    withdrawals: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    rulings: list[dict[str, Any]],
) -> None:
    """Refuse ending a ruling that is not this task's in force, ending one twice, or withdrawing
    one by borrowing the words of a ruling added in the same write.

    An agent at the cap that can add a ruling and withdraw another under one instruction evicts
    an unrelated ruling with words the user said about something else. The store cannot tell
    what the user meant, so it refuses the one reuse it can see.
    """
    added_words = {row["user_instruction"] for row in rulings}
    seen: dict[UUID, str] = {}

    def verify(ident: UUID, verb: str) -> None:
        cur.execute(
            """
            SELECT r.task_id,
                   EXISTS (SELECT 1 FROM ruling_withdrawal w WHERE w.ruling_id = r.ruling_id)
                       AS withdrawn,
                   EXISTS (SELECT 1 FROM ruling_settlement s WHERE s.ruling_id = r.ruling_id)
                       AS settled,
                   EXISTS (SELECT 1 FROM ruling_supersession p WHERE p.supersedes_id = r.ruling_id)
                       AS superseded
            FROM ruling r WHERE r.ruling_id = %s
            """,
            (ident,),
        )
        found = cur.fetchone()
        if found is None:
            raise MashuError(f"no ruling {ident} to {verb}")
        if found["task_id"] != task_id:
            raise MashuError(f"ruling {ident} belongs to another task")
        if found["withdrawn"]:
            raise MashuError(f"ruling {ident} is already withdrawn")
        if found["settled"]:
            raise MashuError(f"ruling {ident} is already settled")
        if found["superseded"]:
            raise MashuError(f"ruling {ident} is already superseded")
        if ident in seen:
            if seen[ident] == verb and verb == "withdraw":
                raise MashuError(f"ruling {ident} is already withdrawn")
            raise MashuError(f"ruling {ident} is ended more than once in this write")
        seen[ident] = verb

    for row in withdrawals:
        ident = row["ruling_id"]
        verify(ident, "withdraw")
        if row["user_instruction"] in added_words:
            raise RefusedError(
                f"the withdrawal of ruling {ident} quotes the same words as a ruling added in "
                "this write. Withdraw a ruling only with what the user said about that ruling"
            )
    for row in settlements:
        verify(row["ruling_id"], "settle")
    for row in rulings:
        for ident in row.get("supersedes") or []:
            verify(ident, "supersede")


def _in_force_count(cur: psycopg.Cursor, task_ids: list[UUID]) -> dict[UUID, int]:
    if not task_ids:
        return {}
    cur.execute(
        """
        SELECT r.task_id, count(*) AS n FROM ruling r
        WHERE r.task_id = ANY(%s)
          AND NOT EXISTS (SELECT 1 FROM ruling_withdrawal w WHERE w.ruling_id = r.ruling_id)
          AND NOT EXISTS (SELECT 1 FROM ruling_settlement s WHERE s.ruling_id = r.ruling_id)
          AND NOT EXISTS (SELECT 1 FROM ruling_supersession p WHERE p.supersedes_id = r.ruling_id)
        GROUP BY r.task_id
        """,
        (task_ids,),
    )
    return {row["task_id"]: row["n"] for row in cur.fetchall()}


def _check_ruling_cap(cur: psycopg.Cursor, task_id: UUID, *, adding: int, ending: int) -> None:
    """Refuse rulings that would leave the task over the user's cap and above where it stands.

    A write that keeps or lowers the count passes even over the cap, since the user may lower
    the setting below what a task already holds.
    """
    if not adding:
        return
    most = settings.limit(cur, "ruling_count")
    current = _in_force_count(cur, [task_id]).get(task_id, 0)
    after = current - ending + adding
    if after <= most or after <= current:
        return
    raise OverLimitError(
        "rulings",
        most,
        after,
        unit="rulings",
        advice=(
            f"the task holds {current} rulings, the most the user set. Settle rulings already "
            "codified in code, docs, a skill, or memory (or finished one-off steps) via "
            "`settle_rulings` with `settled_in`, or link an updated ruling via `supersedes`. "
            "If every remaining ruling is still open or uncodified, ask the user which one no "
            "longer holds and withdraw it in `withdraw_rulings` with their words about it; "
            "never withdraw or settle a rejected option or uncodified constraint on your own "
            "judgment"
        ),
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
    rulings: list[Any] | None = None,
    withdraw_rulings: list[Any] | None = None,
    settle_rulings: list[Any] | None = None,
    artifacts: list[Any] | None = None,
    session: UUID | None = None,
) -> dict[str, Any]:
    """Replace the given state fields and name, and append the rulings and sources with them.

    Every item is checked before the first write, so a refused item leaves the state and
    history as they were. Malformed items and fields over their own ceilings are refused
    together; the ruling cap and the card and detail budgets, which depend on what the task
    holds, come after. No checkpoint row is frozen here: `what_changed` and the ids of the
    rows written are kept on the state-replaced event, and only close freezes a state.
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

    tasks._lock(cur)
    tasks._require_open(cur, task_id)
    now = settings.limits(cur)
    set_limits = {key: now[name] for key, name in SETTING_LIMITS.items()}
    ruling_items, ruling_problems = _items("rulings", rulings, set_limits=set_limits)
    withdrawal_items, withdrawal_problems = _items(
        "withdraw_rulings", withdraw_rulings, set_limits=set_limits
    )
    settlement_items, settlement_problems = _items(
        "settle_rulings", settle_rulings, set_limits=set_limits
    )
    artifact_items, artifact_problems = _items("artifacts", artifacts, set_limits=set_limits)
    current = tasks.task_get(cur, task_id)["state"]
    problems = [
        *_what_changed_problems(what_changed),
        *tasks.limit_problems(tasks.replaced_state(current, **given), current),
        *ruling_problems,
        *withdrawal_problems,
        *settlement_problems,
        *artifact_problems,
    ]
    if problems:
        raise MalformedRequestError(problems)
    verdicts = [tasks._gate({"what_changed": what_changed})]
    for item_name, rows in (
        ("rulings", ruling_items),
        ("withdraw_rulings", withdrawal_items),
        ("settle_rulings", settlement_items),
        ("artifacts", artifact_items),
    ):
        verdicts += [_gate_item(f"{item_name}[{index}]", row) for index, row in enumerate(rows)]
    _check_ruling_endings(cur, task_id, withdrawal_items, settlement_items, ruling_items)
    superseded_ids = [
        supersedes_id for row in ruling_items for supersedes_id in (row.get("supersedes") or [])
    ]
    ending = len(withdrawal_items) + len(settlement_items) + len(superseded_ids)
    _check_ruling_cap(cur, task_id, adding=len(ruling_items), ending=ending)

    # Ids are drawn before the state write so its event can name the rows, while every
    # refusal the state write can raise still comes before the first row is written.
    ruling_ids = [uuid4() for _ in ruling_items]
    reference_ids = [uuid4() for _ in artifact_items]
    history = {
        "what_changed": what_changed,
        "ruling_ids": [str(ident) for ident in ruling_ids],
        "withdrawn_ruling_ids": [str(row["ruling_id"]) for row in withdrawal_items],
        "settled_ruling_ids": [str(row["ruling_id"]) for row in settlement_items],
        "superseded_ruling_ids": [str(ident) for ident in superseded_ids],
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
        rulings_count=current["rulings_count"] - ending + len(ruling_items),
        **given,
    )
    written_rulings = [
        _insert_ruling(
            cur,
            task_id,
            actor=actor,
            ruling_id=ident,
            ruling=item["ruling"],
            user_instruction=item["user_instruction"],
            supersedes=item.get("supersedes") or [],
        )
        for ident, item in zip(ruling_ids, ruling_items, strict=True)
    ]
    written_supersessions = [
        _insert_supersession(
            cur,
            task_id,
            actor=actor,
            ruling_id=ident,
            supersedes_id=supersedes_id,
        )
        for ident, item in zip(ruling_ids, ruling_items, strict=True)
        for supersedes_id in (item.get("supersedes") or [])
    ]
    result = {
        **updated,
        "withdrawn_rulings": [
            _insert_withdrawal(cur, task_id, actor=actor, **item) for item in withdrawal_items
        ],
        "settled_rulings": [
            _insert_settlement(cur, task_id, actor=actor, **item) for item in settlement_items
        ],
        "superseded_rulings": written_supersessions,
        "rulings": written_rulings,
        "artifacts": [
            _insert_artifact(cur, task_id, actor=actor, reference_id=ident, **item)
            for ident, item in zip(reference_ids, artifact_items, strict=True)
        ],
    }
    if ruling_items or withdrawal_items or settlement_items:
        # task_update read the task before these rows existed; the card now names them.
        fresh = tasks.task_get(cur, task_id)
        home = projects.require_project(cur, fresh["task"]["project_id"])
        result.update({**fresh, "card_share": tasks.card_share(cur, home)})
    for verdict in verdicts:
        report = _gate_report(verdict)
        result["unchecked"] = bool(result.get("unchecked")) or report["unchecked"]
        if report.get("malformed"):
            result["malformed"] = max(result.get("malformed", 0), report["malformed"])
    return result


def _insert_ruling(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    ruling_id: UUID,
    ruling: str,
    user_instruction: str,
    supersedes: list[UUID] | None = None,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO ruling (ruling_id, task_id, ruling, user_instruction, created_at, created_by)
        VALUES (%s, %s, %s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (ruling_id, task_id, ruling, user_instruction, actor),
    )
    row = cur.fetchone()
    tasks._renew(cur, task_id)
    detail: dict[str, Any] = {"task_id": str(task_id), "ruling_id": str(ruling_id)}
    if supersedes:
        detail["supersedes_ids"] = [str(ident) for ident in supersedes]
    events.record(cur, "ruling_recorded", actor, detail=detail)
    return row


def _insert_withdrawal(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    ruling_id: UUID,
    user_instruction: str,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO ruling_withdrawal (ruling_id, user_instruction, created_at, created_by)
        VALUES (%s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (ruling_id, user_instruction, actor),
    )
    row = cur.fetchone()
    tasks._renew(cur, task_id)
    events.record(
        cur,
        "ruling_withdrawn",
        actor,
        detail={"task_id": str(task_id), "ruling_id": str(ruling_id)},
    )
    return row


def _insert_settlement(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    ruling_id: UUID,
    settled_in: str,
    note: str | None = None,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO ruling_settlement (ruling_id, settled_in, note, created_at, created_by)
        VALUES (%s, %s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (ruling_id, settled_in, note, actor),
    )
    row = cur.fetchone()
    tasks._renew(cur, task_id)
    events.record(
        cur,
        "ruling_settled",
        actor,
        detail={"task_id": str(task_id), "ruling_id": str(ruling_id), "settled_in": settled_in},
    )
    return row


def _insert_supersession(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    ruling_id: UUID,
    supersedes_id: UUID,
) -> dict[str, Any]:
    cur.execute(
        """
        INSERT INTO ruling_supersession (ruling_id, supersedes_id, created_at, created_by)
        VALUES (%s, %s, clock_timestamp(), %s)
        RETURNING *
        """,
        (ruling_id, supersedes_id, actor),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "ruling_superseded",
        actor,
        detail={
            "task_id": str(task_id),
            "ruling_id": str(supersedes_id),
            "superseded_by": str(ruling_id),
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


def ruling_list(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """A task's rulings newest first, with withdrawal, settlement, and supersession metadata."""
    cur.execute(
        """
        SELECT r.ruling_id, r.ruling, r.user_instruction, r.created_at, r.created_by,
               w.user_instruction AS withdrawn_because, w.created_at AS withdrawn_at,
               st.settled_in, st.note AS settled_note, st.created_at AS settled_at,
               sp.ruling_id AS superseded_by, sp.created_at AS superseded_at,
               ARRAY(
                   SELECT s.supersedes_id FROM ruling_supersession s
                   WHERE s.ruling_id = r.ruling_id ORDER BY s.supersedes_id
               ) AS supersedes_ids,
               CASE
                   WHEN w.ruling_id IS NOT NULL THEN 'withdrawn'
                   WHEN st.ruling_id IS NOT NULL THEN 'settled'
                   WHEN sp.supersedes_id IS NOT NULL THEN 'superseded'
                   ELSE 'in_force'
               END AS status
        FROM ruling r
        LEFT JOIN ruling_withdrawal w ON w.ruling_id = r.ruling_id
        LEFT JOIN ruling_settlement st ON st.ruling_id = r.ruling_id
        LEFT JOIN ruling_supersession sp ON sp.supersedes_id = r.ruling_id
        WHERE r.task_id = %s
        ORDER BY r.created_at DESC, r.ruling_id DESC
        """,
        (task_id,),
    )
    return cur.fetchall()


def rulings_in_force(cur: psycopg.Cursor, task_id: UUID) -> list[dict[str, Any]]:
    """The user's rulings still in force, newest first, each with the user's words.

    All of them come with every task_get: the user's setting bounds how many a task holds, and
    the words beside each ruling are the one check a reader has that the agent's wording of
    it did not carry more than the user said.
    """
    return [
        {
            "ruling_id": row["ruling_id"],
            "on": row["created_at"].date(),
            "ruling": row["ruling"],
            "user_said": row["user_instruction"],
        }
        for row in ruling_list(cur, task_id)
        if row["status"] == "in_force"
    ]


def ruling_count(cur: psycopg.Cursor, task_ids: list[UUID]) -> dict[UUID, int]:
    """How many rulings are in force on each task."""
    return _in_force_count(cur, task_ids)


def _record_counts(cur: psycopg.Cursor, task_id: UUID) -> dict[str, int]:
    cur.execute(
        """
        SELECT (SELECT count(*) FROM attempt WHERE task_id = %(task)s) AS attempts,
               (SELECT count(*) FROM decision WHERE task_id = %(task)s) AS decisions
        """,
        {"task": task_id},
    )
    return dict(cur.fetchone())


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
    withdrawn_rulings: bool = False,
) -> dict[str, Any]:
    """Return the task, its rulings in force, and only the history explicitly pulled with it.

    Attempts and decisions are a record of what agents wrote before rulings; MCP never pulls
    them, and `past_record` only counts them and names where a person reads them.
    """
    expanded = {**tasks.task_get(cur, task_id), "rulings": rulings_in_force(cur, task_id)}
    record = _record_counts(cur, task_id)
    if any(record.values()):
        expanded["past_record"] = {
            **record,
            "read_with": f"mashu task show {task_id}",
            "note": "what agents wrote before rulings existed; a record, not rules in force",
        }
    if withdrawn_rulings:
        all_rulings = ruling_list(cur, task_id)
        expanded["withdrawn_rulings"] = [row for row in all_rulings if row["status"] == "withdrawn"]
        expanded["settled_rulings"] = [row for row in all_rulings if row["status"] == "settled"]
        expanded["superseded_rulings"] = [
            row for row in all_rulings if row["status"] == "superseded"
        ]
    if attempts:
        expanded["attempts"] = attempt_list(cur, task_id)
    if decisions:
        expanded["decisions"] = decision_list(cur, task_id)
    if artifacts:
        expanded["artifacts"] = artifact_list(cur, task_id)
    if checkpoints:
        expanded["checkpoints"] = checkpoint_list(cur, task_id)
    return expanded
