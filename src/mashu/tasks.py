"""Tasks and the one state each of them carries (v3 specification 5.2, 5.3, 7).

This is the subsystem where an agent's writing reaches another session without
a person confirming it, which v2 allowed nowhere. The relaxation is bounded in
three directions and all three live in this module: size, because the store
refuses a field over its limit rather than asking for brevity; time, because a
state whose lease has run out stops being current; and presentation, because
nothing here hands back a state without the date it was last confirmed.

The current state is replaced, never appended to. A task that accumulated its
own history would be a work diary, and a work diary is the artefact v1 proved
a reader's budget cannot survive. What happened instead is kept as history
(checkpoint, attempt, decision), which is written but never delivered.

active and dormant are not stored. They are `open` compared against the lease
at the moment somebody reads, so there is no transition to run and no race
between the two: reactivating is the same act as extending, and silence is
neither completion nor currency.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, projects, redact
from mashu.errors import (
    ClosedTaskError,
    DuplicateTaskError,
    MashuError,
    OverLimitError,
    ProjectBudgetError,
    RefusedError,
    StaleStateError,
)
from mashu.tokens import pushed_cost

#: The three ways a task can end (6). All three are a person's judgement;
#: none of them is something an agent may infer from the work looking done.
OUTCOMES = ("completed", "abandoned", "superseded")

#: Advisory lock class, in the namespace capacity.py opened. Classes 1 and 2
#: there are the seat check and the pain pipeline; this third one serialises
#: everything that reads the project state to decide whether to write it —
#: the duplicate match before a create, and the budget sum before a replace.
#: One class rather than two, because two locks taken in either order is a
#: deadlock waiting for the first day both paths run at once.
LOCK_PROJECT_STATE = 3

#: The ceilings 5.3 puts on one current state. Initial values, to be moved
#: once there is measurement; the refusal names them so a caller never has to
#: guess what it overran.
TEXT_LIMITS = {"goal": 300, "approach": 500, "status_text": 500}
LIST_FIELDS = ("open_questions", "blockers", "next_actions")
LIST_MAX_ITEMS = 5
LIST_MAX_CHARS = 300

_STATE_KEYS = (
    "goal",
    "approach",
    "status_text",
    "open_questions",
    "blockers",
    "next_actions",
    "updated_at",
    "updated_by",
)

#: How a state is titled wherever it is shown. Section 3.1 says a current
#: state is never delivered wearing the face of current truth, and a date in
#: the caller's hands is not the same as a date in the sentence: the heading is
#: built here so that no reading path can print the state without it.
_HEADINGS = {
    "active": "State as of {date}",
    "dormant": "Last known state as of {date}",
    "closed": "Final state as of {date}",
}

_ACTIVITY = """
CASE WHEN t.status = 'closed'    THEN 'closed'
     WHEN now() <= t.active_until THEN 'active'
     ELSE 'dormant' END
"""

_TASK_ROW = f"""
SELECT t.*, p.name AS project_name, {_ACTIVITY} AS activity,
       clock_timestamp() - ts.updated_at AS state_age,
       ts.goal, ts.approach, ts.status_text, ts.open_questions, ts.blockers,
       ts.next_actions, ts.updated_at, ts.updated_by
FROM task t
JOIN project p ON p.project_id = t.project_id
JOIN task_state ts ON ts.task_id = t.task_id
"""


def _floor() -> float:
    """The lowest score worth fetching, on match.py's terms.

    The two thresholds mean what they mean there: SHOW_THRESHOLD is worth a
    glance, match_threshold() is where the code acts alone. Whichever is lower
    is the retrieval floor, so a threshold tuned downward can never be hidden
    by the fetch that feeds it.
    """
    return min(config.SHOW_THRESHOLD, config.match_threshold())


def _lock(cur: psycopg.Cursor) -> None:
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, LOCK_PROJECT_STATE),
    )


def _gate(*texts: str | None) -> redact.Verdict:
    """The entrance check every project-state write passes (v3 10)."""
    verdict = redact.check(*texts)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    return verdict


def _gate_report(verdict: redact.Verdict) -> dict[str, Any]:
    report: dict[str, Any] = {"unchecked": verdict.unchecked}
    if verdict.malformed:
        report["malformed"] = verdict.malformed
    return report


def _check_limits(state: dict[str, Any]) -> None:
    """Refuse an oversized field in words, before the CHECK constraint does.

    The database enforces the same ceilings and that is the enforcement that
    counts. This exists so a caller learns which field it overran and by how
    much, instead of meeting a plpgsql exception with its transaction already
    poisoned behind it.
    """
    for field, limit in TEXT_LIMITS.items():
        value = state.get(field)
        if value and len(value) > limit:
            raise OverLimitError(field, limit, len(value))
    for field in LIST_FIELDS:
        items = state.get(field) or []
        if len(items) > LIST_MAX_ITEMS:
            raise OverLimitError(field, LIST_MAX_ITEMS, len(items), unit="items")
        for item in items:
            if item and len(item) > LIST_MAX_CHARS:
                raise OverLimitError(f"{field} entry", LIST_MAX_CHARS, len(item))


def _clean(value: str | None) -> str | None:
    """Empty is absent. A field cleared by a replacement holds nothing."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_list(items: list[str] | None) -> list[str]:
    return [item.strip() for item in (items or []) if item and item.strip()]


def _state_of(**fields: Any) -> dict[str, Any]:
    return {
        "goal": _clean(fields.get("goal")),
        "approach": _clean(fields.get("approach")),
        "status_text": _clean(fields.get("status_text")),
        "open_questions": _clean_list(fields.get("open_questions")),
        "blockers": _clean_list(fields.get("blockers")),
        "next_actions": _clean_list(fields.get("next_actions")),
    }


def state_text(name: str, state: dict[str, Any]) -> str:
    """What one task costs the opening: its name and its state, as delivered.

    The name is counted with the state because a state arriving without the
    name of the work it belongs to is not deliverable, so the two are one row
    on the wire and one row in the budget.
    """
    parts = [name]
    for field in ("goal", "approach", "status_text"):
        if state.get(field):
            parts.append(state[field])
    for field in LIST_FIELDS:
        parts.extend(state.get(field) or [])
    return "\n".join(parts)


def state_cost(name: str, state: dict[str, Any]) -> int:
    return pushed_cost([state_text(name, state)])


def heading(activity: str, updated_at: dt.datetime) -> str:
    """The line a state is shown under, activity and date together."""
    return _HEADINGS[activity].format(date=updated_at.date().isoformat())


def _split(row: dict[str, Any]) -> dict[str, Any]:
    """One joined row as task, state, and the three things derived from both."""
    extra = set(_STATE_KEYS) | {"activity", "state_age", "score"}
    state = {key: row[key] for key in _STATE_KEYS}
    result = {
        "task": {key: value for key, value in row.items() if key not in extra},
        "state": state,
        "activity": row["activity"],
        "as_of": row["updated_at"].date(),
        "age_days": row["state_age"].days,
        "heading": heading(row["activity"], row["updated_at"]),
    }
    if "score" in row:
        result["score"] = row["score"]
    return result


def task_get(cur: psycopg.Cursor, task_id: UUID) -> dict[str, Any]:
    """One task, its current state, and how current that state actually is.

    `activity` and `heading` are not decoration. A dormant task's state is the
    last thing anybody confirmed, not what is true now, and every path that
    hands one over says which of the two it is holding.
    """
    cur.execute(f"{_TASK_ROW} WHERE t.task_id = %s", (task_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no task {task_id}")
    return _split(row)


def task_list(
    cur: psycopg.Cursor, *, project: UUID | str | None = None, activity: str = "active"
) -> list[dict[str, Any]]:
    """Tasks in one activity, newest activity first.

    'active' is the default because it is the only one that answers "what is
    going on"; the rest are asked for by name. A dormant task is still open
    and still searchable — what it has stopped doing is claiming the present.
    """
    if activity not in ("active", "dormant", "open", "closed", "all"):
        raise MashuError(f"unknown activity '{activity}'")
    project_id = projects.require_project(cur, project)["project_id"] if project else None
    where = {
        "active": "t.status = 'open' AND now() <= t.active_until",
        "dormant": "t.status = 'open' AND now() > t.active_until",
        "open": "t.status = 'open'",
        "closed": "t.status = 'closed'",
        "all": "TRUE",
    }[activity]
    cur.execute(
        f"""
        {_TASK_ROW}
        WHERE {where} AND (%(project)s::uuid IS NULL OR t.project_id = %(project)s::uuid)
        ORDER BY t.last_activity_at DESC, t.task_id
        """,
        {"project": project_id},
    )
    return [_split(row) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# creation, and the match that stops a task being created twice (5.2)
# --------------------------------------------------------------------------

_CANDIDATES = f"""
SELECT * FROM (
    SELECT t.*, p.name AS project_name, {_ACTIVITY} AS activity,
           clock_timestamp() - ts.updated_at AS state_age,
           ts.goal, ts.approach, ts.status_text, ts.open_questions, ts.blockers,
           ts.next_actions, ts.updated_at, ts.updated_by,
           greatest(
               similarity(t.name, %(name)s),
               CASE WHEN %(goal)s = '' OR coalesce(ts.goal, '') = '' THEN 0
                    ELSE similarity(ts.goal, %(goal)s) END
           ) AS score
    FROM task t
    JOIN project p ON p.project_id = t.project_id
    JOIN task_state ts ON ts.task_id = t.task_id
    WHERE t.project_id = %(project)s AND t.status = 'open'
) c
WHERE c.score >= %(floor)s
ORDER BY c.score DESC, c.last_activity_at DESC
LIMIT %(limit)s
"""


def similar_open_tasks(
    cur: psycopg.Cursor, *, project_id: UUID, name: str, goal: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Open tasks in this project whose name or goal reads like this one.

    Dormant tasks are included, and that is the point of matching on `open`
    rather than on the lease: the task an agent is about to duplicate is
    usually one nobody has touched for a fortnight, which is exactly why it
    was not found by looking.
    """
    cur.execute(
        _CANDIDATES,
        {
            "project": project_id,
            "name": name,
            "goal": goal or "",
            "floor": _floor(),
            "limit": limit,
        },
    )
    return [_split(row) for row in cur.fetchall()]


def task_create(
    cur: psycopg.Cursor,
    *,
    project: UUID | str,
    name: str,
    actor: str,
    goal: str | None = None,
    approach: str | None = None,
    status_text: str | None = None,
    open_questions: list[str] | None = None,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Open a task, unless one that reads like it is already open here.

    The match runs before the insert for the reason the nomination queue
    matches before it files: what a lock prevents is two arrivals at once, and
    the duplicate that actually happens arrives a week later, from a session
    whose search for the existing task missed it. Two tasks for one piece of
    work is not a tidiness problem — the state grows in both and neither is
    ever whole.

    `force` is how a caller says it looked and meant it anyway. The candidates
    come back on the exception either way, because continuing one of them is
    the right answer far more often than insisting.
    """
    home = projects.require_project(cur, project)
    name = (name or "").strip()
    if not name:
        raise MashuError("a task needs a name: it is what the duplicate match reads")
    state = _state_of(
        goal=goal,
        approach=approach,
        status_text=status_text,
        open_questions=open_questions,
        blockers=blockers,
        next_actions=next_actions,
    )
    _check_limits(state)
    verdict = _gate(name, *_texts(state))

    # Held from before the match until the insert commits. Between reading
    # "nothing like this exists" and writing the row is exactly where two
    # sessions starting the same work both find nothing.
    _lock(cur)

    candidates = similar_open_tasks(
        cur, project_id=home["project_id"], name=name, goal=state["goal"]
    )
    threshold = config.match_threshold()
    hits = [c for c in candidates if c["score"] >= threshold]
    if hits and not force:
        named = ", ".join(f"{c['task']['name']} ({str(c['task']['task_id'])[:8]})" for c in hits)
        raise DuplicateTaskError(
            f"{len(hits)} open task(s) in '{home['name']}' already read like this: {named}. "
            "Continue one of them, or create this anyway with force.",
            hits,
        )

    _check_budget(cur, task_id=None, name=name, state=state)

    lease = config.task_lease_days()
    cur.execute(
        """
        INSERT INTO task (project_id, name, created_by, last_activity_at, active_until)
        VALUES (%s, %s, %s, now(), now() + make_interval(days => %s::int))
        RETURNING task_id
        """,
        (home["project_id"], name, actor, lease),
    )
    task_id = cur.fetchone()["task_id"]
    cur.execute(
        """
        INSERT INTO task_state (task_id, goal, approach, status_text,
                                open_questions, blockers, next_actions, updated_by)
        VALUES (%(task)s, %(goal)s, %(approach)s, %(status_text)s,
                %(open_questions)s, %(blockers)s, %(next_actions)s, %(actor)s)
        """,
        {"task": task_id, "actor": actor, **state},
    )
    events.record(
        cur,
        "task_created",
        actor,
        detail={
            "task_id": str(task_id),
            "project": home["name"],
            "name": name,
            "forced_over": [str(c["task"]["task_id"]) for c in hits] or None,
        },
    )
    return {**task_get(cur, task_id), "candidates": candidates, **_gate_report(verdict)}


def _texts(state: dict[str, Any]) -> list[str]:
    """Every free-text field of a state, flattened for the entrance check."""
    out = [state.get(field) for field in TEXT_LIMITS]
    for field in LIST_FIELDS:
        out.extend(state.get(field) or [])
    return [text for text in out if text]


# --------------------------------------------------------------------------
# search (5.2, 13.1)
# --------------------------------------------------------------------------

_SEARCH = f"""
SELECT * FROM (
    SELECT t.*, p.name AS project_name, {_ACTIVITY} AS activity,
           clock_timestamp() - ts.updated_at AS state_age,
           ts.goal, ts.approach, ts.status_text, ts.open_questions, ts.blockers,
           ts.next_actions, ts.updated_at, ts.updated_by,
           greatest(
               similarity(t.name, %(query)s),
               similarity(coalesce(ts.goal, ''), %(query)s),
               similarity(coalesce(ts.status_text, ''), %(query)s)
           ) AS score
    FROM task t
    JOIN project p ON p.project_id = t.project_id
    JOIN task_state ts ON ts.task_id = t.task_id
    WHERE (%(project)s::uuid IS NULL OR t.project_id = %(project)s::uuid)
      AND (%(closed)s OR t.status = 'open')
) c
WHERE c.score >= %(floor)s
ORDER BY c.score DESC, c.last_activity_at DESC
LIMIT %(limit)s
"""


def task_search(
    cur: psycopg.Cursor,
    query: str,
    *,
    project: UUID | str | None = None,
    include_closed: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Tasks whose name, goal or status reads like this.

    The one pull path in a subsystem that otherwise pushes. It exists because
    dormant and closed tasks leave the opening but not the store: what stopped
    being delivered has to stay findable, or the lease would be a delete with
    extra steps. Every row carries its activity and its date, so a hit on a
    year-old task cannot be mistaken for a report of the present.
    """
    project_id = projects.require_project(cur, project)["project_id"] if project else None
    cur.execute(
        _SEARCH,
        {
            "query": query,
            "project": project_id,
            "closed": include_closed,
            "floor": _floor(),
            "limit": limit,
        },
    )
    return [_split(row) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# the budget (5.3, 8)
# --------------------------------------------------------------------------

_ACTIVE_STATES = """
SELECT t.task_id, t.name, ts.goal, ts.approach, ts.status_text,
       ts.open_questions, ts.blockers, ts.next_actions
FROM task t JOIN task_state ts ON ts.task_id = t.task_id
WHERE t.status = 'open' AND now() <= t.active_until
"""


def active_state_costs(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """What every active task's state costs to push, heaviest first.

    Across all projects, because the ceiling is on what one session is handed
    and a session is handed the active states, not a project's worth of them.
    """
    cur.execute(_ACTIVE_STATES)
    rows = [
        {"task_id": row["task_id"], "name": row["name"], "tokens": state_cost(row["name"], row)}
        for row in cur.fetchall()
    ]
    return sorted(rows, key=lambda row: row["tokens"], reverse=True)


def _check_budget(
    cur: psycopg.Cursor, *, task_id: UUID | None, name: str, state: dict[str, Any]
) -> None:
    """Refuse a write that would push the Project State share over (5.3).

    The task being written is taken out of the total and put back at its new
    weight, so a state being rewritten never competes with itself. It is added
    rather than replaced when it is dormant or not yet inserted, since a
    successful write renews the lease and makes it active either way.

    Nothing is trimmed and nothing falls through to search. The overflow is
    resolved at the entrance, as it is for memories: something else shrinks,
    goes quiet, or gets closed by the person who owns it.
    """
    cost = state_cost(name, state)
    others = [row for row in active_state_costs(cur) if row["task_id"] != task_id]
    breakdown = sorted(
        [*others, {"task_id": task_id, "name": name, "tokens": cost}],
        key=lambda row: row["tokens"],
        reverse=True,
    )
    total = sum(row["tokens"] for row in breakdown)
    ceiling = config.project_capacity()
    if total <= ceiling:
        return
    raise ProjectBudgetError(
        f"the project state seats {ceiling} tokens and this would take it to {total} "
        f"({cost} for '{name}' on top of {total - cost} already pushed by "
        f"{len(others)} active task(s)). Make room first: shrink another task's state, "
        "leave one alone until its lease runs out, or ask the user to close one "
        "(`mashu task close <id>`).",
        breakdown,
    )


# --------------------------------------------------------------------------
# the current state, and the lease (5.3, 7)
# --------------------------------------------------------------------------


def _require_open(cur: psycopg.Cursor, task_id: UUID) -> dict[str, Any]:
    """The task row, locked, or an error saying a person has ended it.

    FOR UPDATE because every caller reads the row, decides on what it read,
    and writes. Closed is a refusal rather than a no-op: the write was going
    to say something, and swallowing it would leave the writer believing the
    state it is holding is the one being delivered.
    """
    cur.execute("SELECT * FROM task WHERE task_id = %s FOR UPDATE", (task_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no task {task_id}")
    if row["status"] == "closed":
        raise ClosedTaskError(
            f"task '{row['name']}' was closed as {row['outcome']}; "
            "reopening it is the user's call (`mashu task reopen <id>`)"
        )
    return row


def _renew(cur: psycopg.Cursor, task_id: UUID) -> None:
    """Extend the lease from this moment. Every sign of activity comes through here.

    clock_timestamp() for the same reason updated_at uses it: now() is one
    value for a whole transaction, and a renewal that does not move the
    timestamp is indistinguishable from no activity at all.
    """
    cur.execute(
        """
        UPDATE task
        SET last_activity_at = clock_timestamp(),
            active_until = clock_timestamp() + make_interval(days => %s::int)
        WHERE task_id = %s
        """,
        (config.task_lease_days(), task_id),
    )


def task_update(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    actor: str,
    expect_updated_at: dt.datetime,
    goal: str | None = None,
    approach: str | None = None,
    status_text: str | None = None,
    open_questions: list[str] | None = None,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Replace the current state whole, and extend the lease by having done so.

    A replacement, so a field left out is a field cleared: three next actions
    that survived because the caller only mentioned two would be a state
    nobody wrote, and the one thing a current state must not carry is a line
    that is no longer true.

    `expect_updated_at` has no default on purpose. There is no unconditional
    write here, because the failure 5.3 names is not a lost update anybody
    notices — it is a parallel session's work disappearing with nothing left
    to say it was ever there.
    """
    _lock(cur)
    task = _require_open(cur, task_id)
    state = _state_of(
        goal=goal,
        approach=approach,
        status_text=status_text,
        open_questions=open_questions,
        blockers=blockers,
        next_actions=next_actions,
    )
    _check_limits(state)
    verdict = _gate(*_texts(state))

    current = task_get(cur, task_id)
    if current["state"]["updated_at"] != expect_updated_at:
        raise StaleStateError(
            "this state was replaced by "
            f"{current['state']['updated_by']} at {current['state']['updated_at']:%Y-%m-%d %H:%M}; "
            "the version that won is attached — redo the change against it",
            current,
        )

    _check_budget(cur, task_id=task_id, name=task["name"], state=state)

    cur.execute(
        """
        UPDATE task_state
        SET goal = %(goal)s, approach = %(approach)s, status_text = %(status_text)s,
            open_questions = %(open_questions)s, blockers = %(blockers)s,
            next_actions = %(next_actions)s, updated_at = clock_timestamp(),
            updated_by = %(actor)s
        WHERE task_id = %(task)s
        """,
        {"task": task_id, "actor": actor, **state},
    )
    _renew(cur, task_id)
    events.record(
        cur,
        "task_state_replaced",
        actor,
        detail={"task_id": str(task_id), "tokens": state_cost(task["name"], state)},
    )
    return {**task_get(cur, task_id), **_gate_report(verdict)}


def touch(cur: psycopg.Cursor, task_id: UUID, *, actor: str) -> dict[str, Any]:
    """Say the work is still current, without claiming anything about it.

    Also the whole of reactivation (7). Because dormancy is derived rather
    than stored, there is nothing to transition back: a task nobody has
    touched for a fortnight and a task touched a moment ago differ only in
    what now() is compared against.
    """
    _require_open(cur, task_id)
    was = task_get(cur, task_id)["activity"]
    _renew(cur, task_id)
    events.record(cur, "task_touched", actor, detail={"task_id": str(task_id), "was": was})
    return task_get(cur, task_id)


# --------------------------------------------------------------------------
# ending, which is a person's judgement (6)
# --------------------------------------------------------------------------


def close(
    cur: psycopg.Cursor, task_id: UUID, *, outcome: str, actor: str, reason: str | None = None
) -> dict[str, Any]:
    """End a task, on an outcome somebody chose.

    The outcome is required because 'closed' on its own says only that nobody
    is working on this, which is what dormancy already says and says
    reversibly. Whether the work was finished, given up, or replaced is the
    part a later reader cannot reconstruct.

    The state that was current when the task ended is frozen as a final
    checkpoint before the row closes (5.6), so what the work looked like at
    the moment somebody judged it is not lost to the next replacement.
    """
    if outcome not in OUTCOMES:
        raise MashuError(f"outcome must be one of {', '.join(OUTCOMES)}")
    _lock(cur)
    task = _require_open(cur, task_id)
    verdict = _gate(reason)
    what_changed = f"closed as {outcome}" + (f": {reason}" if reason else "")
    from mashu import task_history

    task_history._freeze_current(cur, task_id, actor=actor, what_changed=what_changed)
    cur.execute(
        """
        UPDATE task
        SET status = 'closed', outcome = %s, close_reason = %s, closed_at = now()
        WHERE task_id = %s
        """,
        (outcome, reason, task_id),
    )
    events.record(
        cur,
        "task_closed",
        actor,
        detail={"task_id": str(task_id), "name": task["name"], "outcome": outcome},
    )
    return {**task_get(cur, task_id), **_gate_report(verdict)}


def reopen(cur: psycopg.Cursor, task_id: UUID, *, actor: str) -> dict[str, Any]:
    """Take back a closure, leaving no record of it on the task.

    The outcome is cleared rather than kept as a former one: a task carrying
    'completed' while open would be read by whichever of the two fields the
    reader happened to look at. What it was closed as stays in the event log,
    which is where the account of who decided what belongs anyway.

    The lease is renewed with it, because a task reopened into an expired
    lease would be dormant the instant it came back, and reopening is somebody
    saying this work is current again.
    """
    cur.execute("SELECT * FROM task WHERE task_id = %s FOR UPDATE", (task_id,))
    task = cur.fetchone()
    if task is None:
        raise MashuError(f"no task {task_id}")
    if task["status"] != "closed":
        raise MashuError(f"task '{task['name']}' is already open")
    cur.execute(
        """
        UPDATE task
        SET status = 'open', outcome = NULL, close_reason = NULL, closed_at = NULL,
            last_activity_at = now(), active_until = now() + make_interval(days => %s::int)
        WHERE task_id = %s
        """,
        (config.task_lease_days(), task_id),
    )
    events.record(
        cur,
        "task_reopened",
        actor,
        detail={"task_id": str(task_id), "was": task["outcome"]},
    )
    return task_get(cur, task_id)
