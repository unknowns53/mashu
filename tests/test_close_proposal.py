from __future__ import annotations

import pytest

from conftest import expire, new_project, new_task, update_task
from mashu import task_actions, tasks
from mashu.errors import ClosedTaskError, MashuError, OverLimitError, RefusedError

MERGED = "deleted before the merge d178ecb7, nothing on main references it"


@pytest.fixture
def project(cur):
    return new_project(cur)


@pytest.fixture
def task_id(cur, project):
    made = new_task(
        cur, "drop the close-up tool before the merge", goal="nothing temporary survives into main"
    )
    return made["task"]["task_id"]


def proposed(cur, task_id, outcome="completed", reason=MERGED, actor="agent"):
    return tasks.propose_close(cur, task_id, outcome=outcome, reason=reason, actor=actor)


def proposals(cur, task_id):
    cur.execute("SELECT count(*) AS n FROM task_close_proposal WHERE task_id = %s", (task_id,))
    return cur.fetchone()["n"]


def test_a_proposal_leaves_the_task_open_and_dormant_and_arrives_with_it_wherever_it_is_read(
    cur, task_id, project
):
    assert tasks.task_get(cur, task_id)["proposal"] is None
    expire(cur, task_id)
    row = proposed(cur, task_id)
    assert row["activity"] == "dormant"
    assert row["task"]["status"] == "open"
    assert row["task"]["outcome"] is None
    assert row["proposal"]["outcome"] == "completed"
    assert row["proposal"]["reason"] == MERGED
    assert row["proposal"]["proposed_by"] == "agent"

    assert tasks.task_get(cur, task_id)["proposal"]["outcome"] == "completed"
    listed = tasks.task_list(cur, project=project["project_id"], activity="open")
    assert [row["proposal"]["reason"] for row in listed] == [MERGED]


def test_work_written_after_a_proposal_shows_it_as_overtaken(cur, task_id):
    assert proposed(cur, task_id)["proposal"]["stale"] is False
    update_task(cur, task_id, status_text="turns out the tool is wanted for the deck test")
    assert tasks.task_get(cur, task_id)["proposal"]["stale"] is True


def test_a_new_proposal_replaces_the_standing_one_and_withdrawing_leaves_it_open(cur, task_id):
    proposed(cur, task_id)
    row = proposed(cur, task_id, outcome="superseded", reason="ship-parts took this over")
    assert row["proposal"]["outcome"] == "superseded"
    assert proposals(cur, task_id) == 1

    row = tasks.withdraw_proposal(cur, task_id, actor="user")
    assert row["proposal"] is None
    assert row["task"]["status"] == "open"
    assert proposals(cur, task_id) == 0
    with pytest.raises(MashuError):
        tasks.withdraw_proposal(cur, task_id, actor="user")


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"reason": "   "}, MashuError),
        ({"outcome": "done"}, MashuError),
        ({"reason": "x" * 501}, OverLimitError),
        ({"reason": "merged as SECRETMARKER42"}, RefusedError),
    ],
)
def test_a_proposal_needs_grounds_and_an_outcome_that_pass_every_state_check(
    cur, task_id, change, error
):
    with pytest.raises(error):
        proposed(cur, task_id, **change)


def test_closing_on_the_proposed_outcome_keeps_the_grounds_that_were_written(cur, task_id):
    displayed = proposed(cur, task_id)
    row = task_actions.accept_close_proposal(
        cur,
        task_id,
        expected_proposal=displayed["proposal"],
        expected_state=displayed["state"]["updated_at"],
        actor="user",
    )
    assert row["task"]["close_reason"] == MERGED


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("completed", None), ("completed", "my own words"), ("abandoned", None)],
)
def test_an_explicit_close_records_only_the_reason_typed_at_it(cur, task_id, outcome, reason):
    proposed(cur, task_id)
    row = tasks.close(cur, task_id, outcome=outcome, actor="user", reason=reason)
    assert row["task"]["outcome"] == outcome
    assert row["task"]["close_reason"] == reason


def test_closing_answers_the_proposal_for_good(cur, task_id):
    proposed(cur, task_id)
    tasks.close(cur, task_id, outcome="completed", actor="user")
    assert proposals(cur, task_id) == 0
    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_closed'")
    assert cur.fetchone()["detail"]["proposed"] == "completed"
    with pytest.raises(ClosedTaskError):
        proposed(cur, task_id)
    assert tasks.reopen(cur, task_id, actor="user")["proposal"] is None


@pytest.mark.parametrize("change", ["state", "proposal", "withdraw"])
def test_accepting_a_proposal_rejects_a_snapshot_that_changed(cur, task_id, change):
    displayed = proposed(cur, task_id)
    if change == "state":
        update_task(cur, task_id, status_text="work is needed again")
    elif change == "proposal":
        proposed(cur, task_id, outcome="abandoned", reason="the replacement was cancelled")
    else:
        tasks.withdraw_proposal(cur, task_id, actor="agent")

    with pytest.raises(MashuError, match="changed"):
        task_actions.accept_close_proposal(
            cur,
            task_id,
            expected_proposal=displayed["proposal"],
            expected_state=displayed["state"]["updated_at"],
            actor="user",
        )
    assert tasks.task_get(cur, task_id)["task"]["status"] == "open"
