from __future__ import annotations

import io
import os

import psycopg
import pytest

from mashu import close_ui, db, projects, tasks, work_ui
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_close_parity"
PROPOSAL_REASON = "the replacement has passed its acceptance checks"


@pytest.fixture
def dsn() -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    conninfo = f"dbname={TEST_DB}"
    migrate(conninfo)
    with db.transaction(conninfo) as cur:
        projects.create_project(cur, name="enrai", actor="user")
    return conninfo


@pytest.fixture(autouse=True)
def known_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "40")


def make_task(dsn: str, *, proposal: bool = False, stale: bool = False):
    with db.transaction(dsn) as cur:
        row = tasks.task_create(
            cur,
            project="enrai",
            name="finish the replacement rail",
            goal="fit the approved replacement",
            actor="agent",
            force=True,
        )
        task_id = row["task"]["task_id"]
        if proposal:
            tasks.propose_close(
                cur,
                task_id,
                outcome="completed",
                reason=PROPOSAL_REASON,
                actor="agent",
            )
        if stale:
            state = tasks.task_get(cur, task_id)
            tasks.task_update(
                cur,
                task_id,
                actor="agent",
                expect_updated_at=state["state"]["updated_at"],
                status_text="one last fitting check remains",
            )
        return task_id


def task_row(dsn: str, task_id):
    with db.transaction(dsn) as cur:
        return tasks.task_get(cur, task_id)


def run_entry(dsn: str, task_id, entry: str, monkeypatch, *lines: str) -> None:
    if entry == "work":
        lines = ("c", *lines, "q")

        def run() -> int:
            return work_ui.run(dsn)
    else:

        def run() -> int:
            return close_ui.run(dsn, task_id=task_id)

    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in lines)))
    assert run() == 0


@pytest.mark.parametrize("entry", ["close", "work"])
def test_accepting_a_proposal_has_the_same_result_from_both_entries(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True)
    run_entry(dsn, task_id, entry, monkeypatch, "enter")

    closed = task_row(dsn, task_id)
    assert closed["task"]["outcome"] == "completed"
    assert closed["task"]["close_reason"] == PROPOSAL_REASON


@pytest.mark.parametrize("entry", ["close", "work"])
def test_enter_without_a_proposal_is_a_noop_from_both_entries(dsn, monkeypatch, entry):
    task_id = make_task(dsn)
    run_entry(dsn, task_id, entry, monkeypatch, "enter")

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "open"
    assert row["proposal"] is None


@pytest.mark.parametrize("entry", ["close", "work"])
def test_accepting_a_stale_proposal_uses_the_same_confirmation(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True, stale=True)
    run_entry(dsn, task_id, entry, monkeypatch, "enter", "y")

    assert task_row(dsn, task_id)["task"]["close_reason"] == PROPOSAL_REASON


@pytest.mark.parametrize("entry", ["close", "work"])
def test_a_different_explicit_outcome_does_not_confirm_a_stale_proposal(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True, stale=True)
    run_entry(dsn, task_id, entry, monkeypatch, "a", "")

    closed = task_row(dsn, task_id)
    assert closed["task"]["outcome"] == "abandoned"
    assert closed["task"]["close_reason"] is None


@pytest.mark.parametrize("entry", ["close", "work"])
def test_empty_reason_keeps_proposal_reason_when_outcomes_match(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True)
    run_entry(dsn, task_id, entry, monkeypatch, "c", "")

    assert task_row(dsn, task_id)["task"]["close_reason"] == PROPOSAL_REASON


@pytest.mark.parametrize("entry", ["close", "work"])
def test_ctrl_c_while_entering_a_reason_cancels_the_close(dsn, monkeypatch, entry):
    task_id = make_task(dsn, proposal=True)
    keys = iter(("c", "c", "q") if entry == "work" else ("c",))
    monkeypatch.setattr("sys.stdin", io.StringIO())

    def read(prompt: str = "") -> str:
        if prompt == "> ":
            return next(keys)
        if prompt.startswith("  reason"):
            raise KeyboardInterrupt
        raise AssertionError(f"unexpected prompt: {prompt}")

    monkeypatch.setattr("builtins.input", read)
    if entry == "work":
        assert work_ui.run(dsn) == 0
    else:
        assert close_ui.run(dsn, task_id=task_id) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "open"
    assert row["proposal"]["reason"] == PROPOSAL_REASON


@pytest.mark.parametrize("entry", ["close", "work"])
@pytest.mark.parametrize("change", ["state", "proposal"])
def test_acceptance_rechecks_the_displayed_snapshot(dsn, monkeypatch, entry, change):
    task_id = make_task(dsn, proposal=True, stale=True)

    def change_after_display(_question: str) -> bool:
        with db.transaction(dsn) as cur:
            if change == "state":
                state = tasks.task_get(cur, task_id)
                tasks.task_update(
                    cur,
                    task_id,
                    actor="agent",
                    expect_updated_at=state["state"]["updated_at"],
                    status_text="changed after the close screen opened",
                )
            else:
                tasks.propose_close(
                    cur,
                    task_id,
                    outcome="abandoned",
                    reason="the rail replacement was cancelled",
                    actor="agent",
                )
        return True

    monkeypatch.setattr(close_ui, "_confirm", change_after_display)
    run_entry(dsn, task_id, entry, monkeypatch, "enter")

    current = task_row(dsn, task_id)
    assert current["task"]["status"] == "open"
    assert current["proposal"] is not None
