from __future__ import annotations

import asyncio

import pytest

from conftest import expire, new_project, new_task, update_task
from mashu import db, scopes, server, tasks
from mashu.errors import (
    ClosedTaskError,
    DuplicateTaskError,
    MalformedRequestError,
    MashuError,
    OverLimitError,
    ProjectBudgetError,
    RefusedError,
    StaleStateError,
)

SCHEMA = "implement the v3 schema"
SAME_WORK = "implement the v3 schema migration"
OTHER_WORK = "read up on PNtBAm cononsolvency"
GOAL = "ship migration 0004 with the seven tables"


@pytest.fixture
def project(cur):
    return new_project(cur, "mashu")


@pytest.fixture
def task(cur, project):
    return new_task(
        cur,
        SCHEMA,
        project["project_id"],
        goal=GOAL,
        next_actions=["write the migration", "write the services"],
    )


@pytest.fixture
def task_id(task):
    return task["task"]["task_id"]


def lease_of(cur, task_id):
    cur.execute("SELECT active_until, last_activity_at FROM task WHERE task_id = %s", (task_id,))
    return cur.fetchone()


def count_tasks(cur):
    cur.execute("SELECT count(*) AS n FROM task")
    return cur.fetchone()["n"]


# creation, and the match that keeps one piece of work in one place (5.2)
def test_a_new_task_starts_open_active_and_dated(cur, task):
    assert task["task"]["status"] == "open"
    assert task["task"]["outcome"] is None
    assert task["activity"] == "active"
    assert task["age_days"] == 0
    assert task["heading"] == f"State as of {task['as_of'].isoformat()}"
    assert task["state"]["next_actions"] == ["write the migration", "write the services"]
    assert task["candidates"] == []
    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_created'")
    detail = cur.fetchone()["detail"]
    got = tasks.task_get(cur, task["task"]["task_id"])
    assert got["card_tokens"] == detail["tokens"] == tasks.card_cost(SCHEMA, got["state"])
    assert got["detail_tokens"] == detail["detail_tokens"] == tasks.state_cost(SCHEMA, got["state"])
    assert got["card_remaining"] == got["card_limit"] - got["card_tokens"]


def test_a_task_that_reads_like_an_open_one_even_a_dormant_one_is_created_only_when_forced(
    cur, task_id
):
    with pytest.raises(DuplicateTaskError) as raised:
        new_task(cur, SAME_WORK, "mashu")
    candidates = raised.value.candidates
    assert [c["task"]["task_id"] for c in candidates] == [task_id]
    assert candidates[0]["heading"].startswith("State as of ")
    assert count_tasks(cur) == 1

    expire(cur, task_id)
    with pytest.raises(DuplicateTaskError):
        new_task(cur, SAME_WORK, "mashu")

    forced = new_task(cur, SAME_WORK, "mashu", force=True)
    assert forced["task"]["name"] == SAME_WORK
    assert [c["task"]["task_id"] for c in forced["candidates"]] == [task_id]
    assert count_tasks(cur) == 2


def test_different_work_or_another_project_is_not_a_duplicate(cur, task_id):
    assert new_task(cur, OTHER_WORK, "mashu")["candidates"] == []
    new_project(cur, "thesis")
    assert new_task(cur, SCHEMA, "thesis")["task"]["task_id"] != task_id


def test_a_banned_or_too_large_first_task_never_reaches_the_table(cur, project, monkeypatch):
    with pytest.raises(RefusedError):
        new_task(cur, SCHEMA, "mashu", goal="ask SECRETMARKER9")
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", "20")
    with pytest.raises(ProjectBudgetError):
        new_task(cur, SCHEMA, "mashu", goal=GOAL + " and the module boundary with it")
    assert count_tasks(cur) == 0


# the hard limits (5.3)
@pytest.mark.parametrize("field", tasks.TEXT_LIMITS)
def test_a_field_over_its_limit_is_refused_by_name(cur, task_id, field):
    with pytest.raises(MalformedRequestError) as raised:
        update_task(cur, task_id, **{field: "x" * (tasks.TEXT_LIMITS[field] + 1)})
    limit = tasks.TEXT_LIMITS[field]
    assert raised.value.over_limit == [
        {"field": field, "limit": limit, "actual": limit + 1, "unit": "chars"}
    ]


def test_a_long_state_written_before_the_card_limits_is_carried_but_not_rewritten(cur, task_id):
    cur.execute("UPDATE task_state SET status_text = %s WHERE task_id = %s", ("s" * 400, task_id))
    tasks.append_next_action(cur, task_id, "add the check", actor="agent")
    assert update_task(cur, task_id, blockers=["none"])["state"]["status_text"] == "s" * 400
    with pytest.raises(MalformedRequestError, match="what_changed"):
        update_task(cur, task_id, status_text="t" * 400)


@pytest.mark.parametrize("field", tasks.LIST_FIELDS)
def test_a_list_over_five_entries_or_with_an_entry_over_its_limit_is_refused(cur, task_id, field):
    with pytest.raises(MalformedRequestError) as raised:
        update_task(cur, task_id, **{field: [f"line {n}" for n in range(6)]})
    assert raised.value.over_limit[0]["unit"] == "items"
    with pytest.raises(MalformedRequestError) as raised:
        update_task(cur, task_id, **{field: ["x" * 301]})
    assert raised.value.over_limit[0]["field"] == f"{field}[0]"


# replacement per field, not accumulation (5.3)
def test_an_update_replaces_the_fields_given_and_keeps_the_omitted_ones(cur, task_id):
    update_task(cur, task_id, status_text="tables written", next_actions=["write the services"])
    changed = update_task(cur, task_id, status_text="", next_actions=["wire the MCP tools"])
    assert changed["state"]["next_actions"] == ["wire the MCP tools"]
    assert changed["state"]["status_text"] is None
    assert changed["state"]["goal"] == GOAL

    cur.execute("SELECT count(*) AS n FROM task_state WHERE task_id = %s", (task_id,))
    assert cur.fetchone()["n"] == 1


def test_a_replacement_written_against_a_replaced_state_is_rejected(cur, task):
    task_id = task["task"]["task_id"]
    stale = task["state"]["updated_at"]
    update_task(cur, task_id, actor="the other session", status_text="all seven tables are in")

    with pytest.raises(StaleStateError) as raised:
        update_task(cur, task_id, at=stale, status_text="still writing the tables")

    won = raised.value.current["state"]
    assert won["status_text"] == "all seven tables are in"
    assert won["updated_by"] == "the other session"
    cur.execute("SELECT status_text FROM task_state WHERE task_id = %s", (task_id,))
    assert cur.fetchone()["status_text"] == "all seven tables are in"


# the budget (5.3, 8)
def test_unscoped_state_spends_a_seat_in_every_scope_and_is_weighed_against_the_busiest(
    cur, scope_id, monkeypatch
):
    lighter_scope = scopes.create_scope(cur, name="lighter scope", actor="user")["scope_id"]
    new_project(cur, "busy project", scope_id=scope_id)
    new_project(cur, "light project", scope_id=lighter_scope)
    new_project(cur, "global project")
    busy = new_task(cur, "busy work", "busy project", status_text="x" * 120)
    busy_cost = tasks.card_cost(busy["task"]["name"], busy["state"])
    # A scope already full leaves another scope's share untouched.
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(busy_cost))
    light = new_task(cur, "light work", "light project")

    global_cost = tasks.card_cost("global work", {})
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(busy_cost + global_cost - 1))
    with pytest.raises(ProjectBudgetError) as raised:
        new_task(cur, "global work", "global project")
    assert {row["name"] for row in raised.value.breakdown} == {"busy work", "global work"}

    monkeypatch.delenv("MASHU_PROJECT_CAPACITY")
    global_task = new_task(cur, "global work", "global project")
    global_cost = tasks.card_cost(global_task["task"]["name"], global_task["state"])
    totals = tasks.pushed_totals(cur)
    assert totals["unscoped"] == global_cost
    light_cost = tasks.card_cost(light["task"]["name"], light["state"])
    assert totals["scopes"] == {scope_id: busy_cost, lighter_scope: light_cost}
    assert totals["worst"] == global_cost + busy_cost


def test_a_state_that_would_overflow_the_share_is_refused_until_another_task_goes_dormant(
    cur, task_id, monkeypatch
):
    quiet = new_task(cur, OTHER_WORK, "mashu", goal="find the ternary window")
    seated = sum(row["tokens"] for row in tasks.active_state_costs(cur))
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(seated))
    grown = {
        "goal": GOAL,
        "approach": "one migration, two modules, and the lease derived rather than stored",
        "status_text": "tables written; services and tests still to go before the branch merges",
    }

    with pytest.raises(ProjectBudgetError) as raised:
        update_task(cur, task_id, **grown)

    breakdown = raised.value.breakdown
    assert {row["name"] for row in breakdown} == {SCHEMA, OTHER_WORK}
    assert sum(row["tokens"] for row in breakdown) > seated
    cur.execute("SELECT status_text FROM task_state WHERE task_id = %s", (task_id,))
    assert cur.fetchone()["status_text"] is None

    expire(cur, quiet["task"]["task_id"])
    assert update_task(cur, task_id, **grown)["state"]["status_text"] == grown["status_text"]
    assert [row["name"] for row in tasks.active_state_costs(cur)] == [SCHEMA]


def test_a_store_over_its_ceiling_can_still_be_shrunk_but_not_grown(cur, project, monkeypatch):
    for n in ("alpha", "beta", "gamma"):
        new_task(cur, f"task {n}", project["project_id"], status_text="x" * 120)
    seated = tasks.active_state_costs(cur)
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", str(min(row["tokens"] for row in seated)))

    target = seated[0]["task_id"]
    assert update_task(cur, target, status_text="tiny")["state"]["status_text"] == "tiny"
    with pytest.raises(ProjectBudgetError):
        update_task(cur, seated[1]["task_id"], status_text="x" * 120, goal="and now a goal")


def test_a_task_over_new_card_and_detail_limits_can_still_be_shrunk(cur, project, monkeypatch):
    name, fields = "big", {"goal": "g" * 80, "approach": "a" * 400, "status_text": "s" * 120}
    made = new_task(cur, name, project["project_id"], next_actions=["n" * 250], **fields)
    current = made["state"]
    monkeypatch.setenv("MASHU_TASK_CARD_CAPACITY", str(tasks.card_cost(name, current) - 20))
    monkeypatch.setenv("MASHU_TASK_DETAIL_CAPACITY", str(tasks.state_cost(name, current) - 20))

    shrunk = update_task(
        cur, made["task"]["task_id"], goal="short goal", status_text="short status"
    )

    assert shrunk["state"]["goal"] == "short goal"
    assert shrunk["state"]["approach"] == "a" * 400


# the lease (7)
def test_every_mutating_call_extends_the_lease_and_a_touch_is_the_whole_of_reactivation(
    cur, task_id
):
    expire(cur, task_id)
    before = lease_of(cur, task_id)
    assert tasks.task_get(cur, task_id)["activity"] == "dormant"
    assert tasks.touch(cur, task_id, actor="user")["activity"] == "active"
    after = lease_of(cur, task_id)
    assert after["active_until"] > before["active_until"]
    assert after["last_activity_at"] > before["last_activity_at"]

    expire(cur, task_id)
    assert update_task(cur, task_id, goal="ship migration 0004")["activity"] == "active"

    expire(cur, task_id)
    tasks.close(cur, task_id, outcome="completed", actor="user")
    assert tasks.reopen(cur, task_id, actor="user")["activity"] == "active"

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_touched'")
    assert [row["detail"]["was"] for row in cur.fetchall()] == ["dormant"]


def test_a_dormant_state_is_handed_over_as_the_last_thing_anybody_confirmed(cur, task_id):
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


def test_the_search_reads_the_state_and_finds_what_stopped_being_delivered(cur, task_id):
    found = tasks.task_search(cur, "migration 0004 with the seven tables")
    assert [row["task"]["task_id"] for row in found] == [task_id]

    expire(cur, task_id)
    found = tasks.task_search(cur, "v3 schema")
    assert [row["task"]["task_id"] for row in found] == [task_id]
    assert found[0]["heading"].startswith("Last known state as of ")

    tasks.close(cur, task_id, outcome="abandoned", actor="user")
    assert tasks.task_search(cur, "v3 schema") == []
    closed = tasks.task_search(cur, "v3 schema", include_closed=True)
    assert closed[0]["heading"].startswith("Final state as of ")


# ending, which is a person's judgement (6)
def test_a_closed_task_refuses_every_write(cur, task_id):
    with pytest.raises(MashuError, match="outcome must be one of"):
        tasks.close(cur, task_id, outcome="done", actor="user")
    closed = tasks.close(
        cur, task_id, outcome="completed", reason="merged on the branch", actor="user"
    )
    assert closed["task"]["outcome"] == "completed"
    assert closed["task"]["closed_at"] is not None
    assert closed["activity"] == "closed"

    with pytest.raises(ClosedTaskError):
        update_task(cur, task_id, goal="one more thing")
    with pytest.raises(ClosedTaskError):
        tasks.touch(cur, task_id, actor="agent")
    with pytest.raises(ClosedTaskError):
        tasks.close(cur, task_id, outcome="abandoned", actor="user")
    with pytest.raises(ClosedTaskError):
        tasks.append_next_action(cur, task_id, "too late", actor="agent")


def test_reopening_clears_the_closure_and_leaves_it_in_the_log(cur, task_id):
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


# what a delivered state says about itself (v3 5.3, 8)
def test_full_state_names_every_field_when_explicitly_fetched_and_is_costed_with_labels(cur, task):
    name, state = task["task"]["name"], task["state"]
    text = tasks.state_text(name, state)

    assert text.splitlines()[0] == SCHEMA
    assert f"goal: {GOAL}" in text
    assert "next actions:" in text
    assert "- write the migration" in text
    assert "approach:" not in text  # a field with nothing in it is not labelled
    assert tasks.state_cost(name, state) == tasks.pushed_cost([text])


def test_bootstrap_card_keeps_detail_bodies_out_and_names_what_can_be_fetched(cur, task):
    state = {
        **task["state"],
        "approach": "rewrite the parser around a smaller boundary",
        "status_text": "parser is passing its focused tests",
        "open_questions": ["does the legacy route remain?"],
        "blockers": ["waiting for the fixture"],
        "next_actions": ["run the full suite", "update the specification"],
    }

    card = tasks.card_text(task["task"]["name"], state)

    assert task["task"]["name"] in card
    assert state["goal"] in card and state["status_text"] in card
    assert "approach" in card
    assert "1 open question" in card and "1 blocker" in card
    assert "2 next actions" in card
    for hidden in (
        state["approach"],
        *state["open_questions"],
        *state["blockers"],
        *state["next_actions"],
    ):
        assert hidden not in card
    assert tasks.card_cost(task["task"]["name"], state) == tasks.pushed_cost([card])


def test_task_card_and_full_detail_have_independent_limits(cur, project, monkeypatch):
    name = "bounded task"
    state = tasks._state_of(goal="short goal", status_text="short status")
    monkeypatch.setenv("MASHU_TASK_CARD_CAPACITY", str(tasks.card_cost(name, state) - 1))

    with pytest.raises(OverLimitError) as card_error:
        new_task(cur, name, "mashu", goal=state["goal"], status_text=state["status_text"])
    assert (card_error.value.field, card_error.value.over_by) == ("task bootstrap card", 1)
    assert sum(card_error.value.breakdown.values()) == tasks.card_cost(name, state)

    monkeypatch.delenv("MASHU_TASK_CARD_CAPACITY")
    detail = {"goal": "short goal", "approach": "x" * 400, "next_actions": ["y" * 250, "z" * 250]}
    detail_cost = tasks.state_cost(name, tasks._state_of(**detail))
    monkeypatch.setenv("MASHU_TASK_DETAIL_CAPACITY", str(detail_cost - 1))
    with pytest.raises(OverLimitError) as detail_error:
        new_task(cur, name, "mashu", **detail)
    assert detail_error.value.field == "task detail"


# the one append (v3 5.3, and ledger's work path)
def test_an_append_adds_one_new_action_and_meets_what_a_replacement_would(cur, task_id):
    read_at = tasks.task_get(cur, task_id)["state"]["updated_at"]
    got = tasks.append_next_action(cur, task_id, "add the check", actor="agent")

    assert got["appended"] is True
    assert got["state"]["next_actions"] == [
        "write the migration",
        "write the services",
        "add the check",
    ]
    assert got["state"]["goal"] == GOAL

    again = tasks.append_next_action(cur, task_id, "write the migration", actor="agent")
    assert again["appended"] is False
    assert again["state"]["next_actions"] == got["state"]["next_actions"]

    with pytest.raises(StaleStateError):
        update_task(cur, task_id, at=read_at, status_text="carrying on")

    update_task(cur, task_id, next_actions=[f"action {n}" for n in range(tasks.LIST_MAX_ITEMS)])
    with pytest.raises(MalformedRequestError):
        tasks.append_next_action(cur, task_id, "one too many", actor="agent")


def test_the_append_gates_the_whole_state_not_only_the_new_item(cur, task_id):
    cur.execute(
        "UPDATE task_state SET status_text = %s WHERE task_id = %s",
        ("SECRETMARKER42 slipped in under an older rule", task_id),
    )

    with pytest.raises(RefusedError):
        tasks.append_next_action(cur, task_id, "a perfectly clean action", actor="agent")


# the MCP boundary (5.3)
def test_mcp_task_writes_answer_with_the_card_and_log_a_refused_card(mcp, dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        new_project(cur, "mashu")
    made = mcp("task_create", project="mashu", name=SCHEMA, goal=GOAL, approach="a", blockers=["b"])
    assert set(made["state"]) == {"goal", "status_text", "details", "updated_at", "updated_by"}
    assert (made["state"]["goal"], made["state"]["details"]["blockers"]) == (GOAL, 1)
    assert made["card_remaining"] == made["card_limit"] - made["card_tokens"]

    at, task_id = made["state"]["updated_at"], made["task"]["task_id"]
    monkeypatch.setenv("MASHU_TASK_CARD_CAPACITY", str(made["card_tokens"]))
    refused = mcp("task_update", task_id=task_id, expect_updated_at=at, name=SAME_WORK)
    assert refused["over_by"] == refused["actual"] - refused["limit"] > 0
    assert refused["refusal_recorded"] is True
    with db.transaction(dsn) as cur:
        cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_card_write_refused'")
        assert cur.fetchone()["detail"]["breakdown"] == refused["breakdown"]
        assert tasks.task_get(cur, task_id)["task"]["name"] == SCHEMA


def test_mcp_refuses_a_task_write_carrying_tool_call_markup(mcp, dsn, monkeypatch, tmp_path):
    with db.transaction(dsn) as cur:
        new_project(cur, "mashu")
    made = mcp("task_create", project="mashu", name=SCHEMA)
    task_id, at = made["task"]["task_id"], made["state"]["updated_at"]
    leaked = 'one migration <parameter name="x">two modules'

    listed = mcp("task_update", task_id=task_id, expect_updated_at=at, approach=leaked)
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "absent"))
    unlisted = mcp("task_update", task_id=task_id, expect_updated_at=at, approach=leaked)

    for refused in (listed, unlisted):
        assert refused["ok"] is False and "tool-call markup" in refused["error"]
    with db.transaction(dsn) as cur:
        assert tasks.task_get(cur, task_id)["state"]["approach"] is None


@pytest.mark.parametrize(
    ("tool", "earlier", "minutes_ago", "noted"),
    [
        ("task_update", "same session", 0, True),
        ("task_checkpoint", "same session", 20, True),
        ("task_checkpoint", "other session", 0, False),
        ("task_update", "same session", 31, False),
        ("task_update", "work pain", 0, False),
        ("task_checkpoint", None, 0, False),
    ],
)
def test_a_session_writing_state_again_within_the_spacing_is_told_when_to_write(
    mcp, dsn, tool, earlier, minutes_ago, noted
):
    with db.transaction(dsn) as cur:
        new_project(cur, "mashu")
    made = mcp("task_create", project="mashu", name=SCHEMA, goal=GOAL)
    task_id = made["task"]["task_id"]
    other = server.build_server()
    writers = {
        "same session": mcp,
        "other session": lambda tool, **arguments: (
            asyncio.run(other.call_tool(tool, arguments)).structured_content
        ),
    }
    wrote = {"ok": True}
    if earlier == "work pain":
        wrote = mcp(
            "pain_report",
            kind="incident",
            what="broke",
            prevention=OTHER_WORK,
            prevention_kind="work",
            task_id=task_id,
        )
    elif earlier:
        at = made["state"]["updated_at"]
        wrote = writers[earlier]("task_update", task_id=task_id, expect_updated_at=at, approach="a")
    assert wrote["ok"] is True
    with db.transaction(dsn) as cur:
        cur.execute("ALTER TABLE event_log DISABLE TRIGGER event_log_append_only")
        cur.execute(
            "UPDATE event_log SET created_at = created_at - make_interval(mins => %s)",
            (minutes_ago,),
        )
        cur.execute("ALTER TABLE event_log ENABLE TRIGGER event_log_append_only")
        at = tasks.task_get(cur, task_id)["state"]["updated_at"].isoformat()

    history = {"what_changed": "wrote it"} if tool == "task_checkpoint" else {}
    answer = mcp(tool, task_id=task_id, expect_updated_at=at, status_text="written", **history)
    assert answer["ok"] is True and answer["state"]["status_text"] == "written"
    expected = {"minutes_since_last": minutes_ago, "note": tasks.STATE_WRITE_NOTE}
    assert answer.get("cadence") == (expected if noted else None)
