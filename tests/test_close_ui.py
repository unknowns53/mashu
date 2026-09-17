
from __future__ import annotations

import io
import os

import psycopg
import pytest

from mashu import close_ui, db, projects, tasks
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_close"

MERGED = "deleted before the merge d178ecb7, nothing on main references it"


@pytest.fixture
def dsn() -> str:
    """A database of this test's own, built from the migrations."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    conninfo = f"dbname={TEST_DB}"
    migrate(conninfo)
    with db.transaction(conninfo) as cur:
        projects.create_project(cur, name="enrai", actor="user")
    return conninfo


@pytest.fixture(autouse=True)
def a_screen_of_a_known_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal the paging can be reasoned about, whatever runs the suite."""
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "40")


def keys(monkeypatch: pytest.MonkeyPatch, *typed: str) -> None:
    """Hand the screen its keystrokes: one line each, in the order pressed."""
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in typed)))


def a_task(dsn: str, name: str, *, propose: str | None = None, reason: str = MERGED):
    """One open task, committed, optionally with a proposal standing on it."""
    with db.transaction(dsn) as cur:
        row = tasks.task_create(
            cur, project="enrai", name=name, goal=f"finish {name}", actor="agent"
        )
        task_id = row["task"]["task_id"]
        if propose:
            tasks.propose_close(cur, task_id, outcome=propose, reason=reason, actor="agent")
        return task_id


def task_row(dsn: str, task_id):
    with db.transaction(dsn) as cur:
        return tasks.task_get(cur, task_id)


def test_enter_closes_on_the_proposal_and_keeps_its_grounds(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "enter", "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "closed"
    assert row["task"]["outcome"] == "completed"
    assert row["task"]["close_reason"] == MERGED


def test_the_grounds_are_on_the_screen_before_the_key_that_accepts_them(dsn, monkeypatch, capsys):
    a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "q")

    assert close_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert MERGED in out
    assert "proposed completed by agent" in out


def test_an_outcome_key_asks_for_a_reason_and_takes_an_empty_one(dsn, monkeypatch):
    task_id = a_task(dsn, "rework the hull")
    keys(monkeypatch, "c", "", "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["outcome"] == "completed"
    assert row["task"]["close_reason"] is None


def test_an_empty_reason_keeps_the_proposed_grounds_where_the_outcome_agrees(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "c", "", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["close_reason"] == MERGED


def test_choosing_against_the_proposal_records_no_reason_of_its(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "a", "", "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["outcome"] == "abandoned"
    assert row["task"]["close_reason"] is None


def test_a_typed_reason_is_what_is_recorded(dsn, monkeypatch):
    task_id = a_task(dsn, "rework the hull")
    keys(monkeypatch, "s", "ship-parts took this over", "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["outcome"] == "superseded"
    assert row["task"]["close_reason"] == "ship-parts took this over"


def test_an_overtaken_proposal_is_asked_about_before_it_is_taken(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    with db.transaction(dsn) as cur:
        state = tasks.task_get(cur, task_id)
        tasks.task_update(
            cur,
            task_id,
            actor="agent",
            expect_updated_at=state["state"]["updated_at"],
            status_text="wanted for the deck test after all",
        )
    keys(monkeypatch, "enter", "n", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["status"] == "open"


def test_an_overtaken_proposal_can_still_be_taken_by_saying_so(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    with db.transaction(dsn) as cur:
        state = tasks.task_get(cur, task_id)
        tasks.task_update(
            cur,
            task_id,
            actor="agent",
            expect_updated_at=state["state"]["updated_at"],
            status_text="wanted for the deck test after all",
        )
    keys(monkeypatch, "enter", "y", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["outcome"] == "completed"


def test_enter_on_a_task_nobody_proposed_anything_for_says_so(dsn, monkeypatch, capsys):
    task_id = a_task(dsn, "rework the hull")
    keys(monkeypatch, "enter", "q")

    assert close_ui.run(dsn) == 0
    assert "nothing is proposed" in capsys.readouterr().out
    assert task_row(dsn, task_id)["task"]["status"] == "open"


def test_dropping_a_proposal_leaves_the_task_open_with_its_lease_renewed(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    with db.transaction(dsn) as cur:
        cur.execute(
            "UPDATE task SET active_until = now() - interval '1 day', "
            "last_activity_at = now() - interval '15 days' WHERE task_id = %s",
            (task_id,),
        )
    keys(monkeypatch, "w", "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "open"
    assert row["proposal"] is None
    assert row["activity"] == "active"


def test_renewing_says_nothing_about_whether_the_work_is_finished(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "t", "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["status"] == "open"
    assert row["proposal"]["outcome"] == "completed"


def test_the_proposals_lead_the_list_because_that_is_what_the_screen_is_for(dsn, monkeypatch):
    a_task(dsn, "rework the hull")
    proposed = a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "enter", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, proposed)["task"]["status"] == "closed"


def test_the_arrows_reach_the_tasks_nobody_proposed_anything_for(dsn, monkeypatch):
    unproposed = a_task(dsn, "rework the hull")
    a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "down", "c", "", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, unproposed)["task"]["outcome"] == "completed"


def test_leaving_decides_nothing(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    keys(monkeypatch, "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["status"] == "open"


def test_a_store_with_nothing_open_says_so_rather_than_painting_a_list(dsn, monkeypatch, capsys):
    keys(monkeypatch, "q")
    assert close_ui.run(dsn) == 0
    assert "no open tasks" in capsys.readouterr().out


def test_the_screen_reads_the_dormant_tasks_too(dsn, monkeypatch):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed")
    with db.transaction(dsn) as cur:
        cur.execute(
            "UPDATE task SET active_until = now() - interval '1 day', "
            "last_activity_at = now() - interval '15 days' WHERE task_id = %s",
            (task_id,),
        )
    keys(monkeypatch, "enter", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["status"] == "closed"
