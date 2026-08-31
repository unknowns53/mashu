"""Tasks, their one state, and the lease (v3 specification 5.2, 5.3, 7).

The invariants of section 15 that Phase A can carry: an agent cannot close a
task, silence does not close one either, a dormant state is never handed over
as current truth, and no state is handed over without its date.
"""

from __future__ import annotations

import psycopg
import pytest

from mashu import projects, tasks
from mashu.errors import (
    ClosedTaskError,
    DuplicateTaskError,
    MashuError,
    OverLimitError,
    ProjectBudgetError,
    RefusedError,
    StaleStateError,
)

SCHEMA = "implement the v3 schema"
SAME_WORK = "implement the v3 schema migration"
OTHER_WORK = "read up on PNtBAm cononsolvency"


@pytest.fixture
def project(cur):
    return projects.create_project(cur, name="mashu", actor="user")


@pytest.fixture
def task(cur, project):
    return tasks.task_create(
        cur,
        project=project["project_id"],
        name=SCHEMA,
        goal="ship migration 0004 with the seven tables",
        next_actions=["write the migration", "write the services"],
        actor="agent",
    )


def expire(cur, task_id):
    """Age a task past its lease, which is all dormancy is (7).

    Both columns move, because a fortnight of silence is what the state is
    supposed to look like and now() is one value for the whole test's
    transaction.
    """
    cur.execute(
        """
        UPDATE task
        SET active_until = now() - interval '1 day',
            last_activity_at = now() - interval '15 days'
        WHERE task_id = %s
        """,
        (task_id,),
    )


def lease_of(cur, task_id):
    cur.execute("SELECT active_until, last_activity_at FROM task WHERE task_id = %s", (task_id,))
    return cur.fetchone()


# --------------------------------------------------------------------------
# creation, and the match that keeps one piece of work in one place (5.2)
# --------------------------------------------------------------------------
def test_a_new_task_starts_open_active_and_dated(cur, task):
    assert task["task"]["status"] == "open"
    assert task["task"]["outcome"] is None
    assert task["activity"] == "active"
    assert task["age_days"] == 0
    assert task["heading"] == f"State as of {task['as_of'].isoformat()}"
    assert task["state"]["next_actions"] == ["write the migration", "write the services"]
    assert task["candidates"] == []


def test_a_task_that_reads_like_an_open_one_is_not_created(cur, task):
    """The duplicate that happens is not two sessions at once.

    It is a session a week later whose search missed the task it should have
    continued, and a lock cannot see that one at all. So the match runs before
    the insert, and what comes back is the task to continue.
    """
    with pytest.raises(DuplicateTaskError) as raised:
        tasks.task_create(cur, project="mashu", name=SAME_WORK, actor="agent")

    candidates = raised.value.candidates
    assert [c["task"]["task_id"] for c in candidates] == [task["task"]["task_id"]]
    assert candidates[0]["heading"].startswith("State as of ")

    cur.execute("SELECT count(*) AS n FROM task")
    assert cur.fetchone()["n"] == 1


def test_the_match_reaches_dormant_tasks_because_those_are_the_ones_missed(cur, task):
    expire(cur, task["task"]["task_id"])
    with pytest.raises(DuplicateTaskError):
        tasks.task_create(cur, project="mashu", name=SAME_WORK, actor="agent")


def test_saying_it_anyway_creates_the_second_task(cur, task):
    forced = tasks.task_create(cur, project="mashu", name=SAME_WORK, actor="agent", force=True)
    assert forced["task"]["name"] == SAME_WORK
    assert [c["task"]["task_id"] for c in forced["candidates"]] == [task["task"]["task_id"]]

    cur.execute("SELECT count(*) AS n FROM task")
    assert cur.fetchone()["n"] == 2


def test_different_work_in_the_same_project_is_not_a_duplicate(cur, task):
    other = tasks.task_create(cur, project="mashu", name=OTHER_WORK, actor="agent")
    assert other["candidates"] == []


def test_the_same_name_in_another_project_is_another_task(cur, task):
    projects.create_project(cur, name="thesis", actor="user")
    elsewhere = tasks.task_create(cur, project="thesis", name=SCHEMA, actor="agent")
    assert elsewhere["task"]["task_id"] != task["task"]["task_id"]


def test_a_banned_pattern_is_refused_before_the_task_exists(cur, project):
    with pytest.raises(RefusedError):
        tasks.task_create(cur, project="mashu", name=SCHEMA, goal="ask SECRETMARKER9", actor="a")
    cur.execute("SELECT count(*) AS n FROM task")
    assert cur.fetchone()["n"] == 0


# --------------------------------------------------------------------------
# the hard limits (5.3)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal", "x" * 301),
        ("approach", "x" * 501),
        ("status_text", "x" * 501),
    ],
)
def test_a_field_over_its_limit_is_refused_by_name(cur, task, field, value):
    with pytest.raises(OverLimitError) as raised:
        tasks.task_update(
            cur,
            task["task"]["task_id"],
            actor="agent",
            expect_updated_at=task["state"]["updated_at"],
            **{field: value},
        )
    assert raised.value.field == field
    assert raised.value.actual == len(value)


@pytest.mark.parametrize("field", tasks.LIST_FIELDS)
def test_a_list_over_five_entries_is_refused(cur, task, field):
    with pytest.raises(OverLimitError, match="items"):
        tasks.task_update(
            cur,
            task["task"]["task_id"],
            actor="agent",
            expect_updated_at=task["state"]["updated_at"],
            **{field: [f"line {n}" for n in range(6)]},
        )


@pytest.mark.parametrize("field", tasks.LIST_FIELDS)
def test_a_list_entry_over_its_limit_is_refused(cur, task, field):
    with pytest.raises(OverLimitError) as raised:
        tasks.task_update(
            cur,
            task["task"]["task_id"],
            actor="agent",
            expect_updated_at=task["state"]["updated_at"],
            **{field: ["x" * 301]},
        )
    assert raised.value.field.startswith(field)


def test_the_database_refuses_the_same_lengths_when_the_code_is_gone_round(cur, task):
    """The Python check is for the message; this is the check that counts."""
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE task_state SET goal = %s WHERE task_id = %s",
            ("x" * 301, task["task"]["task_id"]),
        )


def test_the_database_refuses_a_sixth_line_when_the_code_is_gone_round(cur, task):
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE task_state SET blockers = %s WHERE task_id = %s",
            ([f"line {n}" for n in range(6)], task["task"]["task_id"]),
        )


# --------------------------------------------------------------------------
# replacement, not accumulation (5.3)
# --------------------------------------------------------------------------
def test_an_update_replaces_the_state_and_keeps_none_of_the_old_one(cur, task):
    """A work diary is what this subsystem exists not to become.

    A next action that survived because the caller did not mention it is a
    line nobody wrote, standing in a state whose whole claim is currency.
    """
    updated = tasks.task_update(
        cur,
        task["task"]["task_id"],
        actor="agent",
        expect_updated_at=task["state"]["updated_at"],
        goal="ship migration 0004 with the seven tables",
        status_text="tables written, services next",
        next_actions=["write the services"],
    )
    assert updated["state"]["next_actions"] == ["write the services"]
    assert updated["state"]["status_text"] == "tables written, services next"

    cleared = tasks.task_update(
        cur,
        task["task"]["task_id"],
        actor="agent",
        expect_updated_at=updated["state"]["updated_at"],
        goal="ship migration 0004 with the seven tables",
    )
    assert cleared["state"]["next_actions"] == []
    assert cleared["state"]["status_text"] is None

    cur.execute(
        "SELECT count(*) AS n FROM task_state WHERE task_id = %s", (task["task"]["task_id"],)
    )
    assert cur.fetchone()["n"] == 1


def test_a_replacement_written_against_a_replaced_state_is_rejected(cur, task):
    """Last-writer-wins would delete a parallel session's work in silence."""
    stale = task["state"]["updated_at"]
    tasks.task_update(
        cur,
        task["task"]["task_id"],
        actor="the other session",
        expect_updated_at=stale,
        goal="ship migration 0004",
        status_text="all seven tables are in",
    )

    with pytest.raises(StaleStateError) as raised:
        tasks.task_update(
            cur,
            task["task"]["task_id"],
            actor="agent",
            expect_updated_at=stale,
            goal="ship migration 0004",
            status_text="still writing the tables",
        )

    current = raised.value.current
    assert current["state"]["status_text"] == "all seven tables are in"
    assert current["state"]["updated_by"] == "the other session"

    cur.execute("SELECT status_text FROM task_state WHERE task_id = %s", (task["task"]["task_id"],))
    assert cur.fetchone()["status_text"] == "all seven tables are in"


# --------------------------------------------------------------------------
# the budget (5.3, 8)
# --------------------------------------------------------------------------
def test_a_state_that_would_overflow_the_share_is_refused_with_the_breakdown(
    cur, task, monkeypatch
):
    """Nothing is trimmed and nothing falls through to search (8)."""
    tasks.task_create(
        cur, project="mashu", name=OTHER_WORK, goal="find the ternary window", actor="agent"
    )
    seated = sum(row["tokens"] for row in tasks.active_state_costs(cur))
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(seated))

    with pytest.raises(ProjectBudgetError) as raised:
        tasks.task_update(
            cur,
            task["task"]["task_id"],
            actor="agent",
            expect_updated_at=task["state"]["updated_at"],
            goal="ship migration 0004 with the seven tables",
            approach="one migration, two modules, and the lease derived rather than stored",
            status_text="tables written; services and tests still to go before the branch merges",
        )

    breakdown = raised.value.breakdown
    assert {row["name"] for row in breakdown} == {SCHEMA, OTHER_WORK}
    assert sum(row["tokens"] for row in breakdown) > seated
    assert breakdown == sorted(breakdown, key=lambda row: row["tokens"], reverse=True)

    task_id = task["task"]["task_id"]
    cur.execute("SELECT status_text FROM task_state WHERE task_id = %s", (task_id,))
    assert cur.fetchone()["status_text"] is None


def test_a_dormant_task_stops_spending_the_share(cur, task, monkeypatch):
    """The lease is one of the three exits the refusal names (7)."""
    quiet = tasks.task_create(
        cur, project="mashu", name=OTHER_WORK, goal="find the ternary window", actor="agent"
    )
    seated = sum(row["tokens"] for row in tasks.active_state_costs(cur))
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(seated - 1))

    replace = {
        "goal": "ship migration 0004 with the seven tables",
        "next_actions": ["write the migration", "write the services"],
    }
    with pytest.raises(ProjectBudgetError):
        tasks.task_update(
            cur,
            task["task"]["task_id"],
            actor="agent",
            expect_updated_at=task["state"]["updated_at"],
            **replace,
        )

    expire(cur, quiet["task"]["task_id"])
    updated = tasks.task_update(
        cur,
        task["task"]["task_id"],
        actor="agent",
        expect_updated_at=task["state"]["updated_at"],
        **replace,
    )
    assert updated["state"]["goal"] == replace["goal"]
    assert [row["name"] for row in tasks.active_state_costs(cur)] == [SCHEMA]


def test_a_first_task_too_large_for_the_share_never_reaches_the_table(cur, project, monkeypatch):
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", "20")
    with pytest.raises(ProjectBudgetError):
        tasks.task_create(
            cur,
            project="mashu",
            name=SCHEMA,
            goal="ship migration 0004 with the seven tables and the module boundary with it",
            actor="agent",
        )
    cur.execute("SELECT count(*) AS n FROM task")
    assert cur.fetchone()["n"] == 0


# --------------------------------------------------------------------------
# the lease (7)
# --------------------------------------------------------------------------
def test_every_mutating_call_extends_the_lease(cur, task):
    task_id = task["task"]["task_id"]

    expire(cur, task_id)
    assert tasks.task_get(cur, task_id)["activity"] == "dormant"
    touched = tasks.touch(cur, task_id, actor="user")
    assert touched["activity"] == "active"

    expire(cur, task_id)
    updated = tasks.task_update(
        cur,
        task_id,
        actor="agent",
        expect_updated_at=touched["state"]["updated_at"],
        goal="ship migration 0004",
    )
    assert updated["activity"] == "active"

    expire(cur, task_id)
    tasks.close(cur, task_id, outcome="completed", actor="user")
    reopened = tasks.reopen(cur, task_id, actor="user")
    assert reopened["activity"] == "active"

    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'task_touched'")
    assert cur.fetchone()["n"] == 1


def test_a_touch_is_the_whole_of_reactivation(cur, task):
    """There is no transition to run, so reactivating is extending (7)."""
    task_id = task["task"]["task_id"]
    expire(cur, task_id)
    before = lease_of(cur, task_id)

    tasks.touch(cur, task_id, actor="user")
    after = lease_of(cur, task_id)
    assert after["active_until"] > before["active_until"]
    assert after["last_activity_at"] > before["last_activity_at"]

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_touched'")
    assert cur.fetchone()["detail"]["was"] == "dormant"


def test_a_dormant_state_is_handed_over_as_the_last_thing_anybody_confirmed(cur, task):
    """Silence is not completion, and it is not currency either (7, 3.1)."""
    task_id = task["task"]["task_id"]
    expire(cur, task_id)

    dormant = tasks.task_get(cur, task_id)
    assert dormant["activity"] == "dormant"
    assert dormant["task"]["status"] == "open"
    assert dormant["task"]["outcome"] is None
    assert dormant["heading"] == f"Last known state as of {dormant['as_of'].isoformat()}"

    assert tasks.task_list(cur, activity="active") == []
    assert [row["task"]["task_id"] for row in tasks.task_list(cur, activity="dormant")] == [task_id]
    assert [row["task"]["task_id"] for row in tasks.task_list(cur, activity="open")] == [task_id]
    assert tasks.active_state_costs(cur) == []


def test_the_search_finds_what_stopped_being_delivered_and_dates_it(cur, task):
    task_id = task["task"]["task_id"]
    expire(cur, task_id)

    found = tasks.task_search(cur, "v3 schema")
    assert [row["task"]["task_id"] for row in found] == [task_id]
    assert found[0]["heading"].startswith("Last known state as of ")

    tasks.close(cur, task_id, outcome="abandoned", actor="user")
    assert tasks.task_search(cur, "v3 schema") == []
    closed = tasks.task_search(cur, "v3 schema", include_closed=True)
    assert closed[0]["heading"].startswith("Final state as of ")


def test_the_search_reads_the_state_as_well_as_the_name(cur, task):
    found = tasks.task_search(cur, "migration 0004 with the seven tables")
    assert [row["task"]["task_id"] for row in found] == [task["task"]["task_id"]]


# --------------------------------------------------------------------------
# ending, which is a person's judgement (6)
# --------------------------------------------------------------------------
def test_a_closed_task_refuses_every_write(cur, task):
    task_id = task["task"]["task_id"]
    closed = tasks.close(
        cur, task_id, outcome="completed", reason="merged on the branch", actor="user"
    )
    assert closed["task"]["outcome"] == "completed"
    assert closed["task"]["closed_at"] is not None
    assert closed["activity"] == "closed"

    with pytest.raises(ClosedTaskError):
        tasks.task_update(
            cur,
            task_id,
            actor="agent",
            expect_updated_at=closed["state"]["updated_at"],
            goal="one more thing",
        )
    with pytest.raises(ClosedTaskError):
        tasks.touch(cur, task_id, actor="agent")
    with pytest.raises(ClosedTaskError):
        tasks.close(cur, task_id, outcome="abandoned", actor="user")


def test_closing_needs_one_of_the_three_outcomes(cur, task):
    with pytest.raises(MashuError, match="outcome must be one of"):
        tasks.close(cur, task["task"]["task_id"], outcome="done", actor="user")


def test_reopening_clears_the_closure_and_leaves_it_in_the_log(cur, task):
    task_id = task["task"]["task_id"]
    tasks.close(
        cur, task_id, outcome="superseded", reason="folded into the v3 branch", actor="user"
    )

    reopened = tasks.reopen(cur, task_id, actor="user")
    assert reopened["task"]["status"] == "open"
    assert reopened["task"]["outcome"] is None
    assert reopened["task"]["close_reason"] is None
    assert reopened["task"]["closed_at"] is None
    assert reopened["activity"] == "active"

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_reopened'")
    assert cur.fetchone()["detail"]["was"] == "superseded"

    with pytest.raises(MashuError, match="already open"):
        tasks.reopen(cur, task_id, actor="user")


def test_the_database_refuses_a_closure_without_an_outcome(cur, task):
    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE task SET status = 'closed', closed_at = now() WHERE task_id = %s",
            (task["task"]["task_id"],),
        )


# --------------------------------------------------------------------------
# the history tables, which Phase B fills and nothing ever edits (5.4-5.6)
# --------------------------------------------------------------------------
HISTORY = [
    (
        "task_checkpoint",
        "INSERT INTO task_checkpoint (task_id, what_changed, created_by) VALUES (%s, %s, %s)",
        ("the seven tables landed", "agent"),
        "UPDATE task_checkpoint SET what_changed = 'something else'",
    ),
    (
        "attempt",
        "INSERT INTO attempt (task_id, attempt, result, created_by) VALUES (%s, %s, %s, %s)",
        ("split the schema by namespace", "the boundary went unspoken", "agent"),
        "UPDATE attempt SET result = 'it worked'",
    ),
    (
        "decision",
        "INSERT INTO decision (task_id, decision, reason, created_by) VALUES (%s, %s, %s, %s)",
        ("keep one schema", "half the store is already in public", "agent"),
        "UPDATE decision SET reason = 'no reason'",
    ),
    (
        "artifact_reference",
        "INSERT INTO artifact_reference (task_id, kind, locator) VALUES (%s, %s, %s)",
        ("git_commit", "9d12f5a"),
        "UPDATE artifact_reference SET locator = '0000000'",
    ),
]


@pytest.mark.parametrize(("table", "insert", "params", "rewrite"), HISTORY)
def test_the_history_refuses_to_be_rewritten(cur, task, table, insert, params, rewrite):
    """The current state keeps nothing, so this is the only account there is."""
    cur.execute(insert, (task["task"]["task_id"], *params))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute(rewrite)


@pytest.mark.parametrize(("table", "insert", "params", "rewrite"), HISTORY)
def test_the_history_refuses_to_be_deleted(cur, task, table, insert, params, rewrite):
    cur.execute(insert, (task["task"]["task_id"], *params))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("table", [row[0] for row in HISTORY])
def test_the_history_refuses_to_be_emptied(cur, table):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute(f"TRUNCATE {table}")


# --------------------------------------------------------------------------
# what a delivered state says about itself (v3 5.3, 8)
# --------------------------------------------------------------------------


def test_a_delivered_state_names_the_field_each_line_belongs_to(cur, task):
    """An unlabelled pile of paragraphs is delivered, but it is not read.

    The labels live in the body rather than in a printer because the MCP
    caller and the terminal are handed the same string, and the two fields a
    reader is likeliest to confuse — a question nobody has answered and an
    action somebody should take — differ in whether acting on them is right.
    """
    text = tasks.state_text(task["task"]["name"], task["state"])

    assert text.splitlines()[0] == SCHEMA
    assert "goal: ship migration 0004 with the seven tables" in text
    assert "next actions:" in text
    assert "- write the migration" in text
    assert "approach:" not in text  # a field with nothing in it is not labelled


def test_the_labels_are_counted_in_what_the_state_costs(cur, task):
    """The trade is paid at the entrance, not hidden from the budget."""
    state = task["state"]
    name = task["task"]["name"]
    assert tasks.state_cost(name, state) == tasks.pushed_cost([tasks.state_text(name, state)])


# --------------------------------------------------------------------------
# the one append (v3 5.3, and ledger's work path)
# --------------------------------------------------------------------------


def test_an_append_adds_one_action_and_disturbs_nothing_else(cur, task):
    got = tasks.append_next_action(cur, task["task"]["task_id"], "add the check", actor="agent")

    assert got["appended"] is True
    assert got["state"]["next_actions"] == [
        "write the migration",
        "write the services",
        "add the check",
    ]
    assert got["state"]["goal"] == "ship migration 0004 with the seven tables"


def test_an_append_does_not_repeat_what_the_task_already_carries(cur, task):
    got = tasks.append_next_action(
        cur, task["task"]["task_id"], "write the migration", actor="agent"
    )

    assert got["appended"] is False
    assert got["state"]["next_actions"] == ["write the migration", "write the services"]


def test_an_append_is_refused_past_the_ceiling_a_replacement_would_meet(cur, task):
    """The append cannot grow a state the replacement path could not write."""
    task_id = task["task"]["task_id"]
    current = tasks.task_get(cur, task_id)
    tasks.task_update(
        cur,
        task_id,
        actor="agent",
        expect_updated_at=current["state"]["updated_at"],
        next_actions=[f"action {n}" for n in range(tasks.LIST_MAX_ITEMS)],
    )

    with pytest.raises(OverLimitError):
        tasks.append_next_action(cur, task_id, "one too many", actor="agent")


def test_an_append_is_refused_on_a_task_a_person_closed(cur, task):
    tasks.close(cur, task["task"]["task_id"], outcome="completed", actor="user")

    with pytest.raises(ClosedTaskError):
        tasks.append_next_action(cur, task["task"]["task_id"], "too late", actor="agent")


def test_an_append_makes_a_replacement_prepared_before_it_stale(cur, task):
    """Appending is not a way around the optimistic check, it is a write.

    A session that read the state, then had an action appended underneath it,
    must not be able to replace the state and drop that action silently.
    """
    task_id = task["task"]["task_id"]
    read_at = tasks.task_get(cur, task_id)["state"]["updated_at"]
    tasks.append_next_action(cur, task_id, "add the check", actor="agent")

    with pytest.raises(StaleStateError):
        tasks.task_update(
            cur, task_id, actor="agent", expect_updated_at=read_at, status_text="carrying on"
        )


def test_a_store_over_its_ceiling_can_still_be_shrunk(cur, project, monkeypatch):
    """The ceiling is an entrance, not a trap.

    A store can be over without any write having put it there: the ceiling is
    configuration and the cost is computed from how a state is delivered, so
    both move underneath rows nobody touched. From there a flat refusal would
    tell the caller to shrink a task while refusing every shrink, because the
    other active tasks already exceed the ceiling on their own.
    """
    for n in ("alpha", "beta", "gamma"):
        tasks.task_create(
            cur, project=project["project_id"], name=f"task {n}", actor="agent",
            status_text="x" * 240,
        )
    seated = tasks.active_state_costs(cur)
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(min(row["tokens"] for row in seated)))

    target = seated[0]
    at = tasks.task_get(cur, target["task_id"])["state"]["updated_at"]
    got = tasks.task_update(
        cur, target["task_id"], actor="agent", expect_updated_at=at, status_text="tiny"
    )

    assert got["state"]["status_text"] == "tiny"


def test_being_over_the_ceiling_is_not_a_licence_to_grow(cur, project, monkeypatch):
    """Only the direction the ceiling wants is let through."""
    other = tasks.task_create(
        cur, project=project["project_id"], name="task beta", actor="agent",
        status_text="x" * 240,
    )
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", "1")

    at = tasks.task_get(cur, other["task"]["task_id"])["state"]["updated_at"]
    with pytest.raises(ProjectBudgetError):
        tasks.task_update(
            cur, other["task"]["task_id"], actor="agent", expect_updated_at=at,
            status_text="x" * 240, goal="and now a goal as well",
        )


def test_the_append_gates_the_whole_state_not_only_the_new_item(cur, project):
    """Its docstring says the same entrances as a replacement, so prove it."""
    made = tasks.task_create(
        cur, project=project["project_id"], name="task with a secret", actor="agent",
    )
    task_id = made["task"]["task_id"]
    cur.execute(
        "UPDATE task_state SET status_text = %s WHERE task_id = %s",
        ("SECRETMARKER42 slipped in under an older rule", task_id),
    )

    with pytest.raises(RefusedError):
        tasks.append_next_action(cur, task_id, "a perfectly clean action", actor="agent")
