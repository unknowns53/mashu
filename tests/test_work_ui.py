from __future__ import annotations

import pytest

from conftest import committed_task, expire, keys, new_project, task_row, update_task
from mashu import db, projects, routing, scopes, task_history, tasks, work_ui

STATE = {
    "goal": "launch without the old rail",
    "approach": "reuse aft mountings",
    "status_text": "replacement fitted",
    "open_questions": ["does the forward mounting move?"],
    "blockers": ["await load certificate"],
    "next_actions": ["run the loaded trial"],
}

pytestmark = pytest.mark.usefixtures("known_screen")


@pytest.fixture
def dsn(dsn):
    with db.transaction(dsn) as cur:
        new_project(cur)
    return dsn


def make_task(dsn: str, name: str, *, activity: str = "active", proposal: bool = False):
    propose = "completed" if proposal else None
    task_id = committed_task(dsn, name, propose=propose, reason="all acceptance checks passed")
    with db.transaction(dsn) as cur:
        if activity == "dormant":
            expire(cur, task_id)
        elif activity == "closed":
            tasks.close(cur, task_id, outcome="completed", reason="shipped", actor="user")
    return task_id


def listed(dsn: str):
    with db.transaction(dsn) as cur:
        return tasks.task_list(cur)


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
        updated = update_task(cur, task_id, **STATE)
        artifact = task_history.artifact_link(
            cur, task_id, actor="agent", kind="file", locator="reports/rail-load.txt", label="load"
        )
        for attempt, result in (("fit the new rail", "aligned"), ("test the forward mount", "ok")):
            task_history.attempt_record(cur, task_id, actor="agent", attempt=attempt, result=result)
        for decision, reason in (
            ("keep aft mountings", "loads pass"),
            ("document the alternate rail", "future refits need the same dimensions"),
        ):
            task_history.decision_record(
                cur, task_id, actor="agent", decision=decision, reason=reason
            )
        task_history.checkpoint(
            cur,
            task_id,
            actor="agent",
            what_changed="recorded the fitted rail",
            expect_updated_at=updated["state"]["updated_at"],
            evidence=[artifact["reference_id"]],
            **STATE,
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
    assert "size  card" in out and "detail" in out
    for value in STATE.values():
        assert (value[0] if isinstance(value, list) else value) in out
    for text in (
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
    assert [row["task"]["task_id"] for row in listed(dsn)] == [original]

    keys(monkeypatch, "n", "calibrate the same rail", "enrai", "", "y", "q")
    assert work_ui.run(dsn) == 0
    assert len(listed(dsn)) == 2


def test_new_task_is_created_by_the_user(dsn, monkeypatch):
    keys(monkeypatch, "n", "fit the hatch", "", "close without binding", "q")
    assert work_ui.run(dsn) == 0
    row = listed(dsn)[0]
    assert row["task"]["created_by"] == "user"
    assert row["state"]["goal"] == "close without binding"


def test_task_creation_asks_for_a_project_only_when_more_than_one_is_available(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        new_project(cur, "drydock")
    keys(monkeypatch, "n", "build the crane", "", "q")

    assert work_ui.run(dsn) == 0
    assert listed(dsn) == []

    keys(monkeypatch, "n", "build the crane", "drydock", "", "q")
    assert work_ui.run(dsn) == 0
    made = listed(dsn)
    assert len(made) == 1 and made[0]["task"]["project_name"] == "drydock"


def test_task_creation_prefers_the_only_project_in_the_routed_scope(dsn, monkeypatch, tmp_path):
    with db.transaction(dsn) as cur:
        scope_id = scopes.create_scope(cur, name="shipyard", actor="user")["scope_id"]
        new_project(cur, "hull work", scope_id=scope_id)
        routing.add_route(cur, path_prefix=str(tmp_path), scope_id=scope_id, actor="user")
    monkeypatch.chdir(tmp_path)
    keys(monkeypatch, "n", "inspect the keel", "", "", "q")

    assert work_ui.run(dsn) == 0
    made = listed(dsn)
    assert len(made) == 1 and made[0]["task"]["project_name"] == "hull work"


def test_touch_reactivates_a_dormant_task(dsn, monkeypatch):
    task_id = make_task(dsn, "wake the rail", activity="dormant")
    keys(monkeypatch, "t", "q")

    assert work_ui.run(dsn, initial_view="dormant") == 0
    assert task_row(dsn, task_id)["activity"] == "active"


def test_task_name_project_and_current_state_can_be_edited(dsn, monkeypatch):
    task_id = make_task(dsn, "old rail wording")
    with db.transaction(dsn) as cur:
        update_task(cur, task_id, **STATE)
    keys(
        monkeypatch,
        "e",
        "new rail wording",
        "",
        "ship the new rail",
        "-",
        "fitted for trial",
        "load certified? | paint complete?",
        "-",
        "run trial | publish report",
        "q",
    )

    assert work_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert row["task"]["name"] == "new rail wording"
    assert row["task"]["project_name"] == "enrai"
    assert row["state"]["goal"] == "ship the new rail"
    assert row["state"]["approach"] is None
    assert row["state"]["status_text"] == "fitted for trial"
    assert row["state"]["open_questions"] == ["load certified?", "paint complete?"]
    assert row["state"]["blockers"] == []
    assert row["state"]["next_actions"] == ["run trial", "publish report"]


def test_a_task_closed_here_can_be_reopened_from_the_closed_view(dsn, monkeypatch):
    task_id = make_task(dsn, "retire the old rail")
    keys(monkeypatch, "c", "s", "replaced by the carbon rail", "q")
    assert work_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["outcome"] == "superseded"

    keys(monkeypatch, "o", "q")
    assert work_ui.run(dsn, initial_view="closed") == 0
    assert task_row(dsn, task_id)["task"]["status"] == "open"


def test_project_view_shows_counts_and_creates_an_optionally_scoped_project(
    dsn, monkeypatch, capsys
):
    make_task(dsn, "one active task")
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="repository", actor="user")
    keys(monkeypatch, "n", "shipyard", "repository", "q")

    assert work_ui.run(dsn, initial_view="projects") == 0
    with db.transaction(dsn) as cur:
        assert projects.show_project(cur, "shipyard")["scope_name"] == "repository"
    assert "1a  0d  0c" in capsys.readouterr().out


def test_project_name_and_scope_can_be_edited(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="repository", actor="user")
    keys(monkeypatch, "e", "enrai renamed", "repository", "q")

    assert work_ui.run(dsn, initial_view="projects") == 0

    with db.transaction(dsn) as cur:
        assert projects.show_project(cur, "enrai renamed")["scope_name"] == "repository"


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
