from __future__ import annotations

import io

import pytest

from conftest import committed_task, keys, new_project, task_row, update_task
from mashu import close_ui, db, tasks, work_ui

PROPOSAL_REASON = "the replacement has passed its acceptance checks"

pytestmark = [
    pytest.mark.usefixtures("known_screen"),
    pytest.mark.parametrize("entry", ["close", "work"]),
]


@pytest.fixture
def dsn(dsn):
    with db.transaction(dsn) as cur:
        new_project(cur)
    return dsn


def make_task(dsn: str, *, proposal: bool = False, stale: bool = False):
    propose = "completed" if proposal else None
    task_id = committed_task(
        dsn, "fit the replacement rail", propose=propose, reason=PROPOSAL_REASON
    )
    if stale:
        with db.transaction(dsn) as cur:
            update_task(cur, task_id, status_text="one last fitting check remains")
    return task_id


def run(dsn: str, task_id, entry: str) -> int:
    return work_ui.run(dsn) if entry == "work" else close_ui.run(dsn, task_id=task_id)


def run_entry(dsn: str, task_id, entry: str, monkeypatch, *lines: str) -> None:
    keys(monkeypatch, *(("c", *lines, "q") if entry == "work" else lines))
    assert run(dsn, task_id, entry) == 0


def test_accepting_a_proposal_has_the_same_result_from_both_entries(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True)
    run_entry(dsn, task_id, entry, monkeypatch, "enter")

    closed = task_row(dsn, task_id)
    assert closed["task"]["outcome"] == "completed"
    assert closed["task"]["close_reason"] == PROPOSAL_REASON


def test_enter_without_a_proposal_is_a_noop_from_both_entries(dsn, monkeypatch, entry):
    task_id = make_task(dsn)
    run_entry(dsn, task_id, entry, monkeypatch, "enter")

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "open"
    assert row["proposal"] is None


@pytest.mark.parametrize(("answer", "status"), [("y", "closed"), ("n", "open")])
def test_a_stale_proposal_is_taken_only_on_the_same_confirmation(
    dsn, monkeypatch, entry, answer, status
):
    task_id = make_task(dsn, proposal=True, stale=True)
    run_entry(dsn, task_id, entry, monkeypatch, "enter", answer)

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == status
    if status == "closed":
        assert row["task"]["close_reason"] == PROPOSAL_REASON


def test_a_different_explicit_outcome_does_not_confirm_a_stale_proposal(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True, stale=True)
    run_entry(dsn, task_id, entry, monkeypatch, "a", "")

    closed = task_row(dsn, task_id)
    assert closed["task"]["outcome"] == "abandoned"
    assert closed["task"]["close_reason"] is None


@pytest.mark.parametrize(
    ("typed", "recorded"),
    [("", PROPOSAL_REASON), ("ship-parts took this over", "ship-parts took this over")],
)
def test_the_reason_typed_at_an_outcome_key_wins_and_an_empty_one_keeps_the_proposal_grounds(
    dsn, monkeypatch, entry, typed, recorded
):
    task_id = make_task(dsn, proposal=True)
    run_entry(dsn, task_id, entry, monkeypatch, "c", typed)

    assert task_row(dsn, task_id)["task"]["close_reason"] == recorded


def test_ctrl_c_while_entering_a_reason_cancels_the_close(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True)
    pressed = iter(("c", "c", "q") if entry == "work" else ("c",))
    monkeypatch.setattr("sys.stdin", io.StringIO())

    def read(prompt: str = "") -> str:
        if prompt == "> ":
            return next(pressed)
        if prompt.startswith("  reason"):
            raise KeyboardInterrupt
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr("builtins.input", read)
    assert run(dsn, task_id, entry) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "open"
    assert row["proposal"]["reason"] == PROPOSAL_REASON


@pytest.mark.parametrize("change", ["state", "proposal"])
def test_acceptance_rechecks_the_displayed_snapshot(dsn, monkeypatch, entry, change):
    task_id = make_task(dsn, proposal=True, stale=True)

    def change_after_display(_question: str) -> bool:
        with db.transaction(dsn) as cur:
            if change == "state":
                update_task(cur, task_id, status_text="changed after the close screen opened")
            else:
                tasks.propose_close(
                    cur, task_id, outcome="abandoned", reason="it was cancelled", actor="agent"
                )
        return True

    monkeypatch.setattr(close_ui, "_confirm", change_after_display)
    run_entry(dsn, task_id, entry, monkeypatch, "enter")

    current = task_row(dsn, task_id)
    assert current["task"]["status"] == "open"
    assert current["proposal"] is not None
