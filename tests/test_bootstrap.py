"""What a session opens with (specification 6.1, v3 8).

The v3 opening carries three shares that are refused at three separate
entrances, so the counting is checked share by share as well as in total, and
the task states are checked for the two things section 15 asks of them: that
nothing dormant or closed is in there, and that nothing arrives without the
date it was last confirmed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mashu import bootstrap, ledger, memories, projects, scopes, tasks, temporary
from mashu.tokens import pushed_cost

DISCIPLINE = "never report a run as finished without the output that proves it"
LOCAL = "the migration runs before the local server starts"
PINNED = "delegation goes to the reviewer, not to another writer"
THIS_WEEK = "the licence server is offline until Thursday"

SCHEMA = "implement the v3 bootstrap"
OTHER_WORK = "read up on PNtBAm cononsolvency"


@pytest.fixture
def task(cur):
    """One project, one task, one state — the smallest thing that is delivered."""
    projects.create_project(cur, name="mashu", actor="user")
    return tasks.task_create(
        cur,
        project="mashu",
        name=SCHEMA,
        goal="hand the active states over with their dates",
        next_actions=["count the three shares apart"],
        actor="agent",
    )


def expire(cur, task_id):
    """Age a task past its lease, which is the whole of dormancy (7)."""
    cur.execute(
        """
        UPDATE task
        SET active_until = now() - interval '1 day',
            last_activity_at = now() - interval '15 days'
        WHERE task_id = %s
        """,
        (task_id,),
    )


def test_a_rule_for_every_session_reaches_every_session(cur, scope_id):
    memories.remember(cur, content=DISCIPLINE, actor="user")
    blind = bootstrap.session_bootstrap(cur, actor="agent")
    scoped = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)

    assert [row["content"] for row in blind["always"]] == [DISCIPLINE]
    assert [row["content"] for row in scoped["always"]] == [DISCIPLINE]


def test_a_scope_rule_waits_for_a_session_in_that_scope(cur, scope_id):
    """Sending a scope's rules everywhere dilutes the layer everyone does read."""
    memories.remember(cur, content=LOCAL, actor="user", scope_id=scope_id, delivery="scope")
    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]

    assert bootstrap.session_bootstrap(cur, actor="agent")["scoped"] == []
    assert bootstrap.session_bootstrap(cur, actor="agent", scope_id=elsewhere)["scoped"] == []

    here = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert [row["content"] for row in here["scoped"]] == [LOCAL]


def test_a_rule_held_at_the_act_gate_is_not_in_the_opening(cur, scope_id):
    """That is the point of moving it there: it arrives at the decision, not before it."""
    memories.remember(
        cur,
        content=PINNED,
        actor="user",
        scope_id=scope_id,
        delivery="guard",
        guard_action="Task",
    )
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert got["always"] == [] and got["scoped"] == []
    assert got["tokens"] == 0


def test_a_pushed_row_carries_an_id_and_a_body_and_nothing_else(cur):
    """Scaffolding in a fixed-size opening evicts the rules it surrounds."""
    memories.remember(cur, content=DISCIPLINE, actor="user")
    row = bootstrap.session_bootstrap(cur, actor="agent")["always"][0]
    assert set(row) == {"memory_id", "content"}


def test_the_conditions_of_the_week_come_with_the_opening(cur, scope_id):
    temporary.put_temporary(cur, content="the licence server is offline", actor="user", days=3)
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert [row["content"] for row in got["temporary"]] == ["the licence server is offline"]
    assert set(got["temporary"][0]) == {"content", "expires_at"}


def test_a_candidate_waiting_is_worth_one_number(cur):
    """The person has to learn the queue is not empty somewhere, and this costs a line."""
    assert bootstrap.session_bootstrap(cur, actor="agent")["pending"] == 0

    ledger.report_pain(
        cur,
        kind="incident",
        what="the wrong branch was deployed",
        prevention=LOCAL,
        actor="agent",
    )
    assert bootstrap.session_bootstrap(cur, actor="agent")["pending"] == 1


def test_the_opening_reports_each_share_and_the_total(cur, scope_id, task):
    """Three entrances, so three numbers and the sum of them (v3 8).

    One number would say the opening fits while hiding which share is the one
    under pressure, and no share can be relieved by the room another has
    spare.
    """
    memories.remember(cur, content=DISCIPLINE, actor="user")
    memories.remember(cur, content=LOCAL, actor="user", scope_id=scope_id, delivery="scope")
    temporary.put_temporary(cur, content=THIS_WEEK, actor="user", days=3)

    state = task["state"]
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert got["memory_tokens"] == pushed_cost([DISCIPLINE, LOCAL])
    assert got["project_tokens"] == tasks.state_cost(SCHEMA, state)
    assert got["temporary_tokens"] == pushed_cost([THIS_WEEK])
    assert got["tokens"] == got["memory_tokens"] + got["project_tokens"] + got["temporary_tokens"]
    assert got["capacity"] == 4000
    assert got["over_budget"] is False


def test_a_temporary_context_no_longer_spends_the_memory_seats(cur, scope_id):
    """v2 counted it inside the seat count; v3 gives it a room of its own (8)."""
    temporary.put_temporary(cur, content=THIS_WEEK, actor="user", days=3)
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert got["memory_tokens"] == 0
    assert got["temporary_tokens"] == pushed_cost([THIS_WEEK])


def test_an_opening_over_the_total_is_recorded_rather_than_quietly_served(
    cur, scope_id, task, monkeypatch
):
    """Every share is refused at its own door, so the total cannot be exceeded.

    Which is exactly why it is checked here. An invariant nobody observes is
    not an invariant, and if one of the entrances ever stops holding, the
    reading that says so has to exist somewhere other than in this comment.
    """
    memories.remember(cur, content=DISCIPLINE, actor="user")
    monkeypatch.setenv("MASHU_TOTAL_CAPACITY", "10")

    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert got["capacity"] == 10
    assert got["over_budget"] is True
    # Still whole: trimming would drop the standing rules the session opened
    # with, which is the failure the ceiling exists to prevent.
    assert len(got["always"]) == 1 and len(got["states"]) == 1

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'bootstrap_over_capacity'")
    detail = cur.fetchone()["detail"]
    assert detail["tokens"] == got["tokens"] and detail["capacity"] == 10


def test_nothing_is_recorded_when_the_opening_fits(cur, scope_id, task):
    bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'bootstrap_over_capacity'")
    assert cur.fetchone()["n"] == 0


def test_the_state_of_current_work_arrives_under_the_date_it_was_confirmed(cur, task):
    """A current state never wears the face of the present tense (v3 3.1).

    The date is inside the delivered text, not beside it: a heading the client
    is free to drop is not a presentation discipline, it is a hope.
    """
    row = bootstrap.session_bootstrap(cur, actor="agent")["states"][0]
    assert set(row) == {"task", "heading", "content"}
    assert row["task"] == str(task["task"]["task_id"])[:8]
    assert row["heading"] == f"State as of {task['as_of'].isoformat()}"
    assert dt.date.fromisoformat(row["heading"].rsplit(" ", 1)[1]) == task["as_of"]
    assert SCHEMA in row["content"]
    assert "hand the active states over with their dates" in row["content"]


def test_a_task_whose_lease_ran_out_is_not_in_the_opening(cur, task):
    """Silence is not completion, but it is not the present either (7)."""
    expire(cur, task["task"]["task_id"])
    got = bootstrap.session_bootstrap(cur, actor="agent")
    assert got["states"] == []
    assert got["project_tokens"] == 0

    # Still there, and still dated, for anyone who goes looking.
    assert tasks.task_get(cur, task["task"]["task_id"])["heading"].startswith("Last known state")


def test_a_closed_task_is_not_in_the_opening(cur, task):
    tasks.close(cur, task["task"]["task_id"], outcome="completed", actor="user")
    assert bootstrap.session_bootstrap(cur, actor="agent")["states"] == []


def test_the_states_arrive_in_a_fixed_order(cur, task):
    """Most recent work first, and ties broken by id rather than by luck."""
    second = tasks.task_create(cur, project="mashu", name=OTHER_WORK, actor="agent")
    cur.execute(
        "UPDATE task SET last_activity_at = now() - interval '2 days' WHERE task_id = %s",
        (task["task"]["task_id"],),
    )

    got = bootstrap.session_bootstrap(cur, actor="agent")["states"]
    assert [row["task"] for row in got] == [
        str(second["task"]["task_id"])[:8],
        str(task["task"]["task_id"])[:8],
    ]
    assert [row["task"] for row in bootstrap.session_bootstrap(cur, actor="agent")["states"]] == [
        row["task"] for row in got
    ]


def test_work_belonging_to_another_scope_stays_there(cur, scope_id, task):
    """A routed session is handed its own place's work, and the unplaced work.

    A project nobody has put anywhere is not a project for nowhere: excluding
    it would make it visible only to sessions that are themselves unrouted,
    which is the inverse of useful.
    """
    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]
    projects.create_project(cur, name="the other repository", actor="user", scope_id=elsewhere)
    away = tasks.task_create(
        cur, project="the other repository", name="rewrite the exporter", actor="agent"
    )
    here = str(task["task"]["task_id"])[:8]
    there = str(away["task"]["task_id"])[:8]

    at_home = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert [row["task"] for row in at_home["states"]] == [here]

    over_there = bootstrap.session_bootstrap(cur, actor="agent", scope_id=elsewhere)
    assert {row["task"] for row in over_there["states"]} == {here, there}

    # Unrouted: nothing to filter on, and the whole of it is inside the
    # ceiling every one of those writes was refused against anyway.
    unrouted = bootstrap.session_bootstrap(cur, actor="agent")
    assert {row["task"] for row in unrouted["states"]} == {here, there}


def test_what_was_handed_over_is_logged(cur, scope_id):
    memories.remember(cur, content=DISCIPLINE, actor="user")
    got = bootstrap.session_bootstrap(
        cur, actor="agent", scope_id=scope_id, scope_name="test scope", routed=True
    )
    assert got["scope"] == "test scope"
    assert got["routed"] is True

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'bootstrap_served'")
    detail = cur.fetchone()["detail"]
    assert detail["tokens"] == got["tokens"]
    assert detail["scope"] == "test scope"
