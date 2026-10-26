from __future__ import annotations

import pytest

from conftest import (
    committed_task,
    expire,
    keys,
    new_project,
    task_row,
    update_task,
)
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


def test_task_details_show_all_state_proposal_and_latest_history(dsn, monkeypatch, capsys):
    task_id = make_task(dsn, "replace launch rail", proposal=True)
    with db.transaction(dsn) as cur:
        task_history.checkpoint(
            cur,
            task_id,
            actor="agent",
            what_changed="recorded the fitted rail",
            expect_updated_at=tasks.task_get(cur, task_id)["state"]["updated_at"],
            attempts=[
                {"attempt": "fit the new rail", "result": "aligned"},
                {"attempt": "test the forward mount", "result": "ok"},
            ],
            decisions=[
                {"decision": "keep aft mountings", "reason": "loads pass"},
                {"decision": "document the alternate rail", "reason": "refits need it"},
            ],
            artifacts=[
                {"kind": "file", "locator": "reports/rail-load.txt", "label": "load"},
                {"kind": "file", "locator": "reports/rail-dimensions.txt", "label": "dimensions"},
            ],
            **STATE,
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
        "reports/rail-load.txt",
        "reports/rail-dimensions.txt",
    ):
        assert text in out
    assert "Checkpoints" not in out


def test_a_closed_task_shows_the_state_frozen_at_close_with_its_date(dsn, monkeypatch, capsys):
    task_id = make_task(dsn, "retire the old hatch", activity="closed")
    with db.transaction(dsn) as cur:
        frozen = task_history.checkpoint_list(cur, task_id)
    keys(monkeypatch, "3", "enter", "space", "q", "q")

    assert work_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "Checkpoints (1)" in out
    assert "closed as completed: shipped" in out
    assert f"{frozen[0]['created_at'].date().isoformat()}  user" in out


def test_search_reads_state_proposal_and_history_case_insensitively(dsn, monkeypatch, capsys):
    wanted = make_task(dsn, "replace launch rail", proposal=True)
    make_task(dsn, "unrelated deck")
    with db.transaction(dsn) as cur:
        task_history.checkpoint(
            cur,
            wanted,
            actor="agent",
            what_changed="linked the load report",
            expect_updated_at=tasks.task_get(cur, wanted)["state"]["updated_at"],
            artifacts=[
                {
                    "kind": "file",
                    "locator": "reports/Unique-Rail-Load.txt",
                    "label": "Launch Evidence",
                }
            ],
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

    keys(monkeypatch, "n", "calibrate the same rail", "", "close without binding", "y", "q")
    assert work_ui.run(dsn) == 0
    rows = listed(dsn)
    assert len(rows) == 2
    made = next(row for row in rows if row["task"]["task_id"] != original)
    assert made["task"]["created_by"] == "user"
    assert made["state"]["goal"] == "close without binding"


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


def test_task_name_project_and_current_state_can_be_edited(dsn, monkeypatch):
    task_id = make_task(dsn, "old rail wording")
    with db.transaction(dsn) as cur:
        update_task(cur, task_id, **STATE)
    fields = ("new rail wording", "", "ship the new rail", "-", "fitted for trial")
    lists = ("load certified? | paint complete?", "-", "run trial | publish report")
    keys(monkeypatch, "e", *fields, *lists, "q")

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


def test_a_dormant_task_can_be_touched_closed_and_reopened_from_the_closed_view(dsn, monkeypatch):
    task_id = make_task(dsn, "retire the old rail", activity="dormant")
    keys(monkeypatch, "t", "q")
    assert work_ui.run(dsn, initial_view="dormant") == 0
    assert task_row(dsn, task_id)["activity"] == "active"

    keys(monkeypatch, "c", "s", "replaced by the carbon rail", "q")
    assert work_ui.run(dsn) == 0
    assert task_row(dsn, task_id)["task"]["outcome"] == "superseded"

    keys(monkeypatch, "o", "q")
    assert work_ui.run(dsn, initial_view="closed") == 0
    assert task_row(dsn, task_id)["task"]["status"] == "open"


def test_project_view_counts_edits_creates_and_notes_a_refusal(dsn, monkeypatch, capsys):
    make_task(dsn, "one active task")
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="repository", actor="user")
    edit, refused = ("e", "enrai renamed", "repository"), ("n", "SECRETMARKER7 project", "")
    keys(monkeypatch, *edit, *refused, "n", "shipyard", "repository", "q")

    assert work_ui.run(dsn, initial_view="projects") == 0
    out = capsys.readouterr().out
    assert "1a  0d  0c" in out
    assert "matches banned pattern" in out
    with db.transaction(dsn) as cur:
        made = {row["name"]: row["scope_name"] for row in projects.list_projects(cur)}
    assert made == {"enrai renamed": "repository", "shipyard": "repository"}
