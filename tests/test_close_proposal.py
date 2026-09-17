"""An agent's proposal that a task has ended (v3 specification 6).

What section 6 reserves for a person is the deciding, and every test here is
on the line between that and the saying. A proposal changes nothing about
whether a task is open; it is what a person answers in one keystroke instead
of reading the same sentence back out of status_text and retyping it.
"""

from __future__ import annotations

import pytest

from mashu import projects, tasks
from mashu.errors import ClosedTaskError, MashuError, OverLimitError, RefusedError

MERGED = "deleted before the merge d178ecb7, nothing on main references it"


@pytest.fixture
def project(cur):
    return projects.create_project(cur, name="enrai", actor="user")


@pytest.fixture
def task(cur, project):
    return tasks.task_create(
        cur,
        project=project["project_id"],
        name="drop the close-up tool before the merge",
        goal="nothing temporary survives into main",
        actor="agent",
    )


def proposed(cur, task_id, outcome="completed", reason=MERGED, actor="agent"):
    return tasks.propose_close(cur, task_id, outcome=outcome, reason=reason, actor=actor)


def test_a_proposal_leaves_the_task_open(cur, task):
    """The whole of what section 6 reserves: an agent cannot end the work."""
    row = proposed(cur, task["task"]["task_id"])
    assert row["task"]["status"] == "open"
    assert row["task"]["outcome"] is None
    assert row["proposal"]["outcome"] == "completed"
    assert row["proposal"]["reason"] == MERGED
    assert row["proposal"]["proposed_by"] == "agent"


def test_a_proposal_arrives_with_the_task_wherever_one_is_read(cur, task, project):
    """Both reading paths a person drives carry it, or the screen cannot show it."""
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    assert tasks.task_get(cur, task_id)["proposal"]["outcome"] == "completed"
    listed = tasks.task_list(cur, project=project["project_id"], activity="open")
    assert [row["proposal"]["reason"] for row in listed] == [MERGED]


def test_a_task_with_no_proposal_says_so_rather_than_being_absent(cur, task):
    assert tasks.task_get(cur, task["task"]["task_id"])["proposal"] is None


def test_a_proposal_does_not_renew_the_lease(cur, task):
    """A claim that the work stopped must not keep it at the front of the opening."""
    task_id = task["task"]["task_id"]
    cur.execute(
        "UPDATE task SET active_until = now() - interval '1 day', "
        "last_activity_at = now() - interval '15 days' WHERE task_id = %s",
        (task_id,),
    )
    row = proposed(cur, task_id)
    assert row["activity"] == "dormant"


def test_work_written_after_a_proposal_shows_it_as_overtaken(cur, task):
    """The one shape where the screen and the repository are known to disagree."""
    task_id = task["task"]["task_id"]
    assert proposed(cur, task_id)["proposal"]["stale"] is False
    state = tasks.task_get(cur, task_id)
    tasks.task_update(
        cur,
        task_id,
        actor="agent",
        expect_updated_at=state["state"]["updated_at"],
        goal="nothing temporary survives into main",
        status_text="turns out the tool is wanted for the deck test",
    )
    assert tasks.task_get(cur, task_id)["proposal"]["stale"] is True


def test_proposing_again_replaces_the_one_standing(cur, task):
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    row = proposed(cur, task_id, outcome="superseded", reason="ship-parts took this over")
    assert row["proposal"]["outcome"] == "superseded"
    cur.execute("SELECT count(*) AS n FROM task_close_proposal WHERE task_id = %s", (task_id,))
    assert cur.fetchone()["n"] == 1


def test_a_proposal_needs_the_grounds_with_it(cur, task):
    """An outcome with nothing under it is the work this exists to remove."""
    with pytest.raises(MashuError):
        proposed(cur, task["task"]["task_id"], reason="   ")


def test_an_outcome_that_is_not_one_of_the_three_is_refused(cur, task):
    with pytest.raises(MashuError):
        proposed(cur, task["task"]["task_id"], outcome="done")


def test_an_oversized_reason_names_the_field_it_overran(cur, task):
    with pytest.raises(OverLimitError):
        proposed(cur, task["task"]["task_id"], reason="x" * 501)


def test_a_proposal_passes_the_same_gate_every_state_write_does(cur, task):
    with pytest.raises(RefusedError):
        proposed(cur, task["task"]["task_id"], reason="merged as SECRETMARKER42")


def test_a_closed_task_cannot_be_proposed_about(cur, task):
    task_id = task["task"]["task_id"]
    tasks.close(cur, task_id, outcome="completed", actor="user")
    with pytest.raises(ClosedTaskError):
        proposed(cur, task_id)


def test_closing_on_the_proposed_outcome_keeps_the_grounds_that_were_written(cur, task):
    """The sentence being agreed with is the sentence recorded."""
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    row = tasks.close(cur, task_id, outcome="completed", actor="user")
    assert row["task"]["close_reason"] == MERGED


def test_a_reason_typed_at_the_close_wins_over_the_proposed_one(cur, task):
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    row = tasks.close(cur, task_id, outcome="completed", actor="user", reason="my own words")
    assert row["task"]["close_reason"] == "my own words"


def test_deciding_against_the_proposal_does_not_record_its_reason(cur, task):
    """A different outcome is the person disagreeing; the grounds are not theirs."""
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    row = tasks.close(cur, task_id, outcome="abandoned", actor="user")
    assert row["task"]["outcome"] == "abandoned"
    assert row["task"]["close_reason"] is None


def test_closing_answers_the_proposal_and_takes_it_away(cur, task):
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    tasks.close(cur, task_id, outcome="completed", actor="user")
    cur.execute("SELECT count(*) AS n FROM task_close_proposal WHERE task_id = %s", (task_id,))
    assert cur.fetchone()["n"] == 0


def test_the_event_log_says_a_close_answered_a_proposal(cur, task):
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    tasks.close(cur, task_id, outcome="completed", actor="user")
    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_closed'")
    assert cur.fetchone()["detail"]["proposed"] == "completed"


def test_withdrawing_leaves_the_task_exactly_as_it_was(cur, task):
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    row = tasks.withdraw_proposal(cur, task_id, actor="user")
    assert row["proposal"] is None
    assert row["task"]["status"] == "open"


def test_withdrawing_when_nothing_stands_says_so(cur, task):
    with pytest.raises(MashuError):
        tasks.withdraw_proposal(cur, task["task"]["task_id"], actor="user")


def test_a_reopened_task_does_not_get_its_answered_proposal_back(cur, task):
    """The proposal was answered. Reopening is a new question, not the old one."""
    task_id = task["task"]["task_id"]
    proposed(cur, task_id)
    tasks.close(cur, task_id, outcome="completed", actor="user")
    assert tasks.reopen(cur, task_id, actor="user")["proposal"] is None
