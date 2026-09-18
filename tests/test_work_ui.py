from __future__ import annotations

import io
import os

import psycopg
import pytest

from mashu import db, projects, scopes, task_history, tasks, work_ui
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_work_ui"


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


def keys(monkeypatch: pytest.MonkeyPatch, *typed: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in typed)))


def make_task(
    dsn: str,
    name: str,
    *,
    activity: str = "active",
    proposal: bool = False,
):
    with db.transaction(dsn) as cur:
        made = tasks.task_create(
            cur,
            project="enrai",
            name=name,
            goal=f"finish {name}",
            actor="agent",
            force=True,
        )
        task_id = made["task"]["task_id"]
        if proposal:
            tasks.propose_close(
                cur,
                task_id,
                outcome="completed",
                reason="all acceptance checks passed",
                actor="agent",
            )
        if activity == "dormant":
            cur.execute(
                "UPDATE task SET active_until = now() - interval '1 day' WHERE task_id = %s",
                (task_id,),
            )
        elif activity == "closed":
            tasks.close(cur, task_id, outcome="completed", reason="shipped", actor="user")
        return task_id


def get_task(dsn: str, task_id):
    with db.transaction(dsn) as cur:
        return tasks.task_get(cur, task_id)


def test_four_views_can_be_opened_and_initial_view_is_honoured(dsn, monkeypatch, capsys):
    make_task(dsn, "active hull")
    make_task(dsn, "dormant rail", activity="dormant")
    make_task(dsn, "closed hatch", activity="closed")
    keys(monkeypatch, "1", "2", "3", "4", "q")

    assert work_ui.run(dsn, initial_view="projects") == 0
    out = capsys.readouterr().out
    assert "active hull" in out
    assert "dormant rail" in out
    assert "closed hatch" in out
    assert "enrai" in out and "1a  1d  1c" in out


def test_task_details_show_all_state_proposal_and_latest_history(dsn, monkeypatch, capsys):
    task_id = make_task(dsn, "replace launch rail", proposal=True)
    with db.transaction(dsn) as cur:
        row = tasks.task_get(cur, task_id)
        updated = tasks.task_update(
            cur,
            task_id,
            actor="agent",
            expect_updated_at=row["state"]["updated_at"],
            goal="launch without the old rail",
            approach="reuse aft mountings",
            status_text="replacement fitted",
            open_questions=["does the forward mounting move?"],
            blockers=["await load certificate"],
            next_actions=["run the loaded trial"],
        )
        artifact = task_history.artifact_link(
            cur,
            task_id,
            actor="agent",
            kind="file",
            locator="reports/rail-load.txt",
            label="load report",
        )
        task_history.attempt_record(
            cur, task_id, actor="agent", attempt="fit the new rail", result="aligned"
        )
        task_history.attempt_record(
            cur,
            task_id,
            actor="agent",
            attempt="test the forward mount",
            result="also aligned",
        )
        task_history.decision_record(
            cur, task_id, actor="agent", decision="keep aft mountings", reason="loads pass"
        )
        task_history.decision_record(
            cur,
            task_id,
            actor="agent",
            decision="document the alternate rail",
            reason="future refits need the same dimensions",
        )
        task_history.checkpoint(
            cur,
            task_id,
            actor="agent",
            what_changed="recorded the fitted rail",
            expect_updated_at=updated["state"]["updated_at"],
            goal="launch without the old rail",
            approach="reuse aft mountings",
            status_text="replacement fitted",
            open_questions=["does the forward mounting move?"],
            blockers=["await load certificate"],
            next_actions=["run the loaded trial"],
            evidence=[artifact["reference_id"]],
        )
        task_history.artifact_link(
            cur,
            task_id,
            actor="agent",
            kind="file",
            locator="reports/rail-dimensions.txt",
            label="dimension report",
        )
    keys(monkeypatch, "enter", "space", "q", "q")

    assert work_ui.run(dsn) == 0
    out = capsys.readouterr().out
    for text in (
        "launch without the old rail",
        "reuse aft mountings",
        "replacement fitted",
        "does the forward mounting move?",
        "await load certificate",
        "run the loaded trial",
        "Proposal",
        "STALE",
        "fit the new rail",
        "test the forward mount",
        "keep aft mountings",
        "document the alternate rail",
        "recorded the fitted rail",
        "reports/rail-load.txt",
        "reports/rail-dimensions.txt",
    ):
        assert text in out


def test_search_reads_state_proposal_and_history_case_insensitively(dsn, monkeypatch, capsys):
    wanted = make_task(dsn, "replace launch rail", proposal=True)
    make_task(dsn, "unrelated deck")
    with db.transaction(dsn) as cur:
        task_history.artifact_link(
            cur,
            wanted,
            actor="agent",
            kind="file",
            locator="reports/Unique-Rail-Load.txt",
            label="Launch Evidence",
        )
        row = task_history.expanded_task(cur, wanted, artifacts=True)
    assert work_ui._matches_task(row, "unique-rail")
    assert work_ui._matches_task(row, "LAUNCH EVIDENCE")

    keys(monkeypatch, "/", "UNIQUE-RAIL", "left", "q")
    assert work_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "1/2 active task(s)" in out
    assert "search 'UNIQUE-RAIL'" in out


def test_duplicate_task_is_forced_only_after_explicit_confirmation(dsn, monkeypatch):
    original = make_task(dsn, "calibrate the same rail")
    keys(monkeypatch, "n", "calibrate the same rail", "enrai", "", "n", "q")
    assert work_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        assert [_task["task"]["task_id"] for _task in tasks.task_list(cur)] == [original]

    keys(monkeypatch, "n", "calibrate the same rail", "enrai", "", "y", "q")
    assert work_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        assert len(tasks.task_list(cur)) == 2


def test_new_task_is_created_by_the_user_and_remains_selected(dsn, monkeypatch, capsys):
    keys(monkeypatch, "n", "fit the hatch", "enrai", "close without binding", "q")
    assert work_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        row = tasks.task_list(cur)[0]
    assert row["task"]["created_by"] == "user"
    assert row["state"]["goal"] == "close without binding"
    assert "fit the hatch" in capsys.readouterr().out


def test_touch_reactivates_a_dormant_task(dsn, monkeypatch):
    task_id = make_task(dsn, "wake the rail", activity="dormant")
    keys(monkeypatch, "t", "q")

    assert work_ui.run(dsn, initial_view="dormant") == 0
    assert get_task(dsn, task_id)["activity"] == "active"


def test_close_can_accept_a_standing_proposal(dsn, monkeypatch):
    task_id = make_task(dsn, "finish the rail", proposal=True)
    keys(monkeypatch, "c", "y", "q")

    assert work_ui.run(dsn) == 0
    closed = get_task(dsn, task_id)
    assert closed["task"]["outcome"] == "completed"
    assert closed["task"]["close_reason"] == "all acceptance checks passed"


def test_stale_proposal_needs_a_second_confirmation(dsn, monkeypatch, capsys):
    task_id = make_task(dsn, "finish the rail", proposal=True)
    with db.transaction(dsn) as cur:
        row = tasks.task_get(cur, task_id)
        tasks.task_update(
            cur,
            task_id,
            actor="agent",
            expect_updated_at=row["state"]["updated_at"],
            status_text="one more load test is needed",
        )
    keys(monkeypatch, "c", "y", "n", "q")

    assert work_ui.run(dsn) == 0
    assert get_task(dsn, task_id)["task"]["status"] == "open"
    assert "stale proposal was not confirmed" in capsys.readouterr().out


def test_task_can_be_closed_without_a_proposal_and_reopened(dsn, monkeypatch):
    task_id = make_task(dsn, "retire the old rail")
    keys(monkeypatch, "c", "s", "replaced by the carbon rail", "q")
    assert work_ui.run(dsn) == 0
    closed = get_task(dsn, task_id)
    assert closed["task"]["outcome"] == "superseded"
    assert closed["task"]["close_reason"] == "replaced by the carbon rail"

    keys(monkeypatch, "o", "q")
    assert work_ui.run(dsn, initial_view="closed") == 0
    assert get_task(dsn, task_id)["task"]["status"] == "open"


def test_project_view_shows_counts_and_creates_an_optionally_scoped_project(
    dsn, monkeypatch, capsys
):
    make_task(dsn, "one active task")
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="repository", actor="user")
    keys(monkeypatch, "n", "shipyard", "repository", "q")

    assert work_ui.run(dsn, initial_view="projects") == 0
    with db.transaction(dsn) as cur:
        made = projects.show_project(cur, "shipyard")
    assert made["scope_name"] == "repository"
    out = capsys.readouterr().out
    assert "1a  0d  0c" in out
    assert "shipyard" in out


def test_refused_project_write_becomes_a_note_and_changes_nothing(dsn, monkeypatch, capsys):
    keys(monkeypatch, "n", "SECRETMARKER7 project", "", "q")

    assert work_ui.run(dsn, initial_view="projects") == 0
    with db.transaction(dsn) as cur:
        assert [row["name"] for row in projects.list_projects(cur)] == ["enrai"]
    assert "matches banned pattern" in capsys.readouterr().out


def test_navigation_zero_results_and_non_tty_output_stay_bounded(dsn, monkeypatch, capsys):
    for number in range(12):
        make_task(dsn, f"task {number:02}")
    monkeypatch.setenv("LINES", "14")
    keys(monkeypatch, "end", "home", "pagedown", "pageup", "/", "absent", "/", "", "q")

    assert work_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "0/12 active task(s)" in out
    assert "no matches" in out
    assert "\x1b[" not in out
    # Each repaint is independently bounded even though redirected output retains all of them.
    assert max(len(block.splitlines()) for block in out.split("> ")) <= 15
