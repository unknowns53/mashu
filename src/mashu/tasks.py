"""Manage tasks, current state, leases, and close proposals."""

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

#: The three ways a task can end (6).
OUTCOMES = ("completed", "abandoned", "superseded")

#: Advisory lock class, in the namespace capacity.py opened.
LOCK_PROJECT_STATE = 3

#: The ceilings 5.3 puts on one current state.
TEXT_LIMITS = {"goal": 300, "approach": 500, "status_text": 500}
LIST_FIELDS = ("open_questions", "blockers", "next_actions")
LIST_MAX_ITEMS = 5
LIST_MAX_CHARS = 300

#: What each field is called wherever a state is delivered.
EDITABLE_FIELDS = ("goal", "approach", "status_text", *LIST_FIELDS)

STATE_LABELS = {
    "goal": "goal",
    "approach": "approach",
    "status_text": "status",
    "open_questions": "open questions",
    "blockers": "blockers",
    "next_actions": "next actions",
}

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

#: How a state is titled wherever it is shown.
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

#: Join close proposals into task reads and task listings.
_PROPOSAL_COLUMNS = """
       cp.outcome AS proposed_outcome, cp.reason AS proposed_reason,
       cp.state_at AS proposed_against, cp.proposed_at, cp.proposed_by
"""
_PROPOSAL_JOIN = "LEFT JOIN task_close_proposal cp ON cp.task_id = t.task_id"

_TASK_ROW = f"""
SELECT t.*, p.name AS project_name, {_ACTIVITY} AS activity,
       clock_timestamp() - ts.updated_at AS state_age,
       ts.goal, ts.approach, ts.status_text, ts.open_questions, ts.blockers,
       ts.next_actions, ts.updated_at, ts.updated_by,
{_PROPOSAL_COLUMNS}
FROM task t
JOIN project p ON p.project_id = t.project_id
JOIN task_state ts ON ts.task_id = t.task_id
{_PROPOSAL_JOIN}
"""


def _floor() -> float:
    """The lowest score worth fetching, on match.py's terms."""
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
    """Refuse an oversized field in words, before the CHECK constraint does."""
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
    """What one task costs the opening: its name and its state, as delivered."""
    parts = [name]
    for field in ("goal", "approach", "status_text"):
        if state.get(field):
            parts.append(f"{STATE_LABELS[field]}: {state[field]}")
    for field in LIST_FIELDS:
        items = state.get(field) or []
        if items:
            parts.append(f"{STATE_LABELS[field]}:")
            parts.extend(f"- {item}" for item in items)
    return "\n".join(parts)


def state_cost(name: str, state: dict[str, Any]) -> int:
    return pushed_cost([state_text(name, state)])


def heading(activity: str, updated_at: dt.datetime) -> str:
    """The line a state is shown under, activity and date together."""
    return _HEADINGS[activity].format(date=updated_at.date().isoformat())


#: Proposal columns removed from the task dictionary in `_split`.
_PROPOSAL_KEYS = (
    "proposed_outcome",
    "proposed_reason",
    "proposed_against",
    "proposed_at",
    "proposed_by",
)


def _proposal(row: dict[str, Any]) -> dict[str, Any] | None:
    """The standing proposal that this task has ended, if an agent left one."""
    if not row.get("proposed_outcome"):
        return None
    return {
        "outcome": row["proposed_outcome"],
        "reason": row["proposed_reason"],
        "proposed_at": row["proposed_at"],
        "proposed_by": row["proposed_by"],
        "on_date": row["proposed_at"].date(),
        "stale": row["updated_at"] > row["proposed_against"],
    }


def _split(row: dict[str, Any]) -> dict[str, Any]:
    """One joined row as task, state, and the things derived from both."""
    extra = set(_STATE_KEYS) | set(_PROPOSAL_KEYS) | {"activity", "state_age", "score"}
    state = {key: row[key] for key in _STATE_KEYS}
    result = {
        "task": {key: value for key, value in row.items() if key not in extra},
        "state": state,
        "activity": row["activity"],
        "as_of": row["updated_at"].date(),
        "age_days": row["state_age"].days,
        "heading": heading(row["activity"], row["updated_at"]),
        "proposal": _proposal(row),
    }
    if "score" in row:
        result["score"] = row["score"]
    return result


def task_get(cur: psycopg.Cursor, task_id: UUID) -> dict[str, Any]:
    """One task, its current state, and how current that state actually is."""
    cur.execute(f"{_TASK_ROW} WHERE t.task_id = %s", (task_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no task {task_id}")
    return _split(row)


def task_list(
    cur: psycopg.Cursor, *, project: UUID | str | None = None, activity: str = "active"
) -> list[dict[str, Any]]:
    """Tasks in one activity, newest activity first."""
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


# creation, and the match that stops a task being created twice (5.2)

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
    """Open tasks in this project whose name or goal reads like this one."""
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
    """Open a task, unless one that reads like it is already open here."""
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

    # Held from before the match until the insert commits.
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

    _check_budget(cur, task_id=None, name=name, state=state, scope_id=home["scope_id"])

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


# search (5.2, 13.1)

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
    """Tasks whose name, goal or status reads like this."""
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


# the budget (5.3, 8)

_ACTIVE_STATES = """
SELECT t.task_id, t.name, p.scope_id, ts.goal, ts.approach, ts.status_text,
       ts.open_questions, ts.blockers, ts.next_actions
FROM task t
JOIN project p ON p.project_id = t.project_id
JOIN task_state ts ON ts.task_id = t.task_id
WHERE t.status = 'open' AND now() <= t.active_until
"""


def active_state_costs(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """What every active task's state costs to push, heaviest first."""
    cur.execute(_ACTIVE_STATES)
    rows = [
        {
            "task_id": row["task_id"],
            "name": row["name"],
            "scope_id": row["scope_id"],
            "tokens": state_cost(row["name"], row),
        }
        for row in cur.fetchall()
    ]
    return sorted(rows, key=lambda row: row["tokens"], reverse=True)


def pushed_totals(cur: psycopg.Cursor) -> dict[str, Any]:
    """What active state costs everywhere, per scope, and at its worst."""
    rows = active_state_costs(cur)
    unscoped = sum(row["tokens"] for row in rows if row["scope_id"] is None)
    scoped: dict[UUID, int] = {}
    for row in rows:
        if row["scope_id"] is not None:
            scoped[row["scope_id"]] = scoped.get(row["scope_id"], 0) + row["tokens"]
    return {
        "unscoped": unscoped,
        "scopes": scoped,
        "worst": unscoped + max(scoped.values(), default=0),
        "count": len(rows),
    }


def _budget_competitors(
    rows: list[dict[str, Any]], *, scope_id: UUID | None
) -> tuple[list[dict[str, Any]], str]:
    """The states delivered beside a write, using the heaviest scope for global state."""
    unscoped = [row for row in rows if row["scope_id"] is None]
    if scope_id is not None:
        scoped = [row for row in rows if row["scope_id"] == scope_id]
        return [*unscoped, *scoped], "this scope"

    buckets: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        if row["scope_id"] is not None:
            buckets.setdefault(row["scope_id"], []).append(row)
    heaviest = max(
        buckets.values(), key=lambda bucket: sum(r["tokens"] for r in bucket), default=[]
    )
    return [*unscoped, *heaviest], "the busiest scope"


def _check_budget(
    cur: psycopg.Cursor,
    *,
    task_id: UUID | None,
    name: str,
    state: dict[str, Any],
    scope_id: UUID | None,
) -> None:
    """Refuse a write that would push its scope's Project State share over (5.3)."""
    cost = state_cost(name, state)
    seated = active_state_costs(cur)
    was = next((row["tokens"] for row in seated if row["task_id"] == task_id), None)
    others, boundary = _budget_competitors(
        [row for row in seated if row["task_id"] != task_id], scope_id=scope_id
    )
    reported = [
        {"task_id": row["task_id"], "name": row["name"], "tokens": row["tokens"]}
        for row in others
    ]
    breakdown = sorted(
        [*reported, {"task_id": task_id, "name": name, "tokens": cost}],
        key=lambda row: row["tokens"],
        reverse=True,
    )
    total = sum(row["tokens"] for row in breakdown)
    ceiling = config.project_capacity()
    if total <= ceiling:
        return
    if was is not None and cost < was:
        return
    raise ProjectBudgetError(
        f"the project state share for {boundary} seats {ceiling} tokens and this would "
        f"take it to {total} "
        f"({cost} for '{name}' on top of {total - cost} already pushed by "
        f"{len(others)} active task(s)). A write that makes this task's own state "
        "smaller is allowed through even from here, so shrink this one; otherwise "
        "leave a task alone until its lease runs out, or ask the user to close one "
        "(`mashu task close <id>`).",
        breakdown,
    )


# the current state, and the lease (5.3, 7)


def _require_open(cur: psycopg.Cursor, task_id: UUID) -> dict[str, Any]:
    """The task row, locked, or an error saying a person has ended it."""
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
    """Extend the lease from this moment. Every sign of activity comes through here."""
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
    """Replace the current state whole, and extend the lease by having done so."""
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

    home = projects.require_project(cur, task["project_id"])
    _check_budget(cur, task_id=task_id, name=task["name"], state=state, scope_id=home["scope_id"])

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


def append_next_action(
    cur: psycopg.Cursor, task_id: UUID, text: str, *, actor: str
) -> dict[str, Any]:
    """Add one next action, leaving every other field as it stands."""
    text = (text or "").strip()
    if not text:
        raise MashuError("a next action needs something in it")
    _lock(cur)
    task = _require_open(cur, task_id)
    current = task_get(cur, task_id)
    state = _state_of(**{field: current["state"][field] for field in EDITABLE_FIELDS})
    if text in state["next_actions"]:
        return {**current, "appended": False, "reason": "this next action is already on the task"}
    state["next_actions"] = [*state["next_actions"], text]
    _check_limits(state)
    verdict = _gate(*_texts(state))
    home = projects.require_project(cur, task["project_id"])
    _check_budget(cur, task_id=task_id, name=task["name"], state=state, scope_id=home["scope_id"])

    cur.execute(
        "UPDATE task_state SET next_actions = %s, updated_at = clock_timestamp(), "
        "updated_by = %s WHERE task_id = %s",
        (state["next_actions"], actor, task_id),
    )
    _renew(cur, task_id)
    events.record(
        cur,
        "task_next_action_appended",
        actor,
        detail={"task_id": str(task_id), "tokens": state_cost(task["name"], state)},
    )
    return {**task_get(cur, task_id), **_gate_report(verdict), "appended": True}


def touch(cur: psycopg.Cursor, task_id: UUID, *, actor: str) -> dict[str, Any]:
    """Say the work is still current, without claiming anything about it."""
    _require_open(cur, task_id)
    was = task_get(cur, task_id)["activity"]
    _renew(cur, task_id)
    events.record(cur, "task_touched", actor, detail={"task_id": str(task_id), "was": was})
    return task_get(cur, task_id)


# ending, which is a person's judgement (6)

#: What a proposed reason may run to.
PROPOSAL_REASON_MAX = 500


def propose_close(
    cur: psycopg.Cursor, task_id: UUID, *, outcome: str, reason: str, actor: str
) -> dict[str, Any]:
    """Say that this task looks ended, without ending it."""
    if outcome not in OUTCOMES:
        raise MashuError(f"outcome must be one of {', '.join(OUTCOMES)}")
    reason = (reason or "").strip()
    if not reason:
        raise MashuError(
            "a proposal needs the grounds with it: what makes this look finished, "
            "given up, or replaced"
        )
    if len(reason) > PROPOSAL_REASON_MAX:
        raise OverLimitError("reason", PROPOSAL_REASON_MAX, len(reason))
    _lock(cur)
    task = _require_open(cur, task_id)
    verdict = _gate(reason)
    current = task_get(cur, task_id)
    cur.execute(
        """
        INSERT INTO task_close_proposal
               (task_id, outcome, reason, state_at, proposed_at, proposed_by)
        VALUES (%s, %s, %s, %s, clock_timestamp(), %s)
        ON CONFLICT (task_id) DO UPDATE
        SET outcome = EXCLUDED.outcome, reason = EXCLUDED.reason,
            state_at = EXCLUDED.state_at, proposed_at = EXCLUDED.proposed_at,
            proposed_by = EXCLUDED.proposed_by
        """,
        (task_id, outcome, reason, current["state"]["updated_at"], actor),
    )
    events.record(
        cur,
        "task_close_proposed",
        actor,
        detail={"task_id": str(task_id), "name": task["name"], "outcome": outcome},
    )
    return {**task_get(cur, task_id), **_gate_report(verdict)}


def withdraw_proposal(cur: psycopg.Cursor, task_id: UUID, *, actor: str) -> dict[str, Any]:
    """Take a proposal back, leaving the task exactly as it was."""
    _require_open(cur, task_id)
    cur.execute(
        "DELETE FROM task_close_proposal WHERE task_id = %s RETURNING outcome",
        (task_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise MashuError("no close proposal stands on this task")
    events.record(
        cur,
        "task_close_proposal_withdrawn",
        actor,
        detail={"task_id": str(task_id), "was": row["outcome"]},
    )
    return task_get(cur, task_id)


def close(
    cur: psycopg.Cursor, task_id: UUID, *, outcome: str, actor: str, reason: str | None = None
) -> dict[str, Any]:
    """End a task, on an outcome somebody chose."""
    if outcome not in OUTCOMES:
        raise MashuError(f"outcome must be one of {', '.join(OUTCOMES)}")
    _lock(cur)
    task = _require_open(cur, task_id)
    cur.execute(
        "DELETE FROM task_close_proposal WHERE task_id = %s RETURNING outcome, reason",
        (task_id,),
    )
    proposal = cur.fetchone()
    if reason is None and proposal is not None and proposal["outcome"] == outcome:
        reason = proposal["reason"]
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
        detail={
            "task_id": str(task_id),
            "name": task["name"],
            "outcome": outcome,
            "proposed": proposal["outcome"] if proposal else None,
        },
    )
    return {**task_get(cur, task_id), **_gate_report(verdict)}


def reopen(cur: psycopg.Cursor, task_id: UUID, *, actor: str) -> dict[str, Any]:
    """Take back a closure, leaving no record of it on the task."""
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
