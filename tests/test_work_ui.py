from __future__ import annotations

import pytest

from conftest import (
    assert_steady_layout,
    committed_task,
    expire,
    keys,
    new_project,
    task_row,
    update_task,
)
from mashu import db, projects, routing, scopes, screen, task_history, tasks, work_ui

STATE = {
    "goal": "launch without the old rail",
    "approach": "reuse aft mountings",
    "status_text": "replacement fitted",
    "open_questions": ["does the forward mounting move?"],
    "blockers": ["await load certificate"],
    "next_actions": ["run the loaded trial"],
}

STEPS = [f"step {n} of the loaded trial and its checks" for n in range(5)]

pytestmark = pytest.mark.usefixtures("known_screen")


def test_unchanged_list_keeps_a_pipe_inside_an_item(monkeypatch):
    current = ["check printf x | tool"]
    monkeypatch.setattr(screen, "editline", lambda _prompt, initial: screen.Submitted(initial))
    assert work_ui._replacement_list("next actions", current) == current
    monkeypatch.setattr(screen, "editline", lambda *_args: screen.Submitted("-"))
    assert work_ui._replacement_list("next actions", current) == []


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
        "recorded the fitted rail",
        "reports/rail-load.txt",
        "reports/rail-dimensions.txt",
    ):
        assert text in out


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


def test_q_quits_from_the_list_from_details_and_from_help(dsn, monkeypatch, capsys):
    make_task(dsn, "active hull")
    for pressed in (("q",), ("enter", "q"), ("?", "q")):
        keys(monkeypatch, *pressed)
        with pytest.raises(screen.Quit):
            work_ui.run.__wrapped__(dsn)
    out = capsys.readouterr().out
    assert out.startswith("Work\n")
    assert "Work help" in out and "reopen the selected closed task" in out


def test_a_24_line_terminal_lists_eight_tasks_before_the_preview(dsn, monkeypatch, capsys):
    names = [f"task {number:02}" for number in range(8)]
    for name in names:
        task_id = make_task(dsn, name)
        with db.transaction(dsn) as cur:
            update_task(cur, task_id, **STATE)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    keys(monkeypatch, "q")

    assert work_ui.run(dsn) == 0
    first = capsys.readouterr().out.split("> ")[0]
    assert all(name in first for name in names)
    assert "more line(s); ⏎ reads all" in first
    assert len(first.splitlines()) <= 24


def test_history_fields_sit_under_their_entry_and_wrap_under_the_value(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    row = {
        "artifacts": [],
        "checkpoints": [{"created_at": None, "created_by": "agent", "what_changed": "word " * 12}],
    }
    lines = work_ui._history_lines(row)
    assert lines[0] == "  Checkpoints (1)"
    assert lines[1].startswith("    -  agent")
    assert lines[2].startswith("      changed   word")
    assert all(line.startswith(" " * 16) and line[16] != " " for line in lines[3:])


def test_moving_the_cursor_moves_neither_the_list_nor_the_keys(dsn, monkeypatch):
    names = ["alpha hull", "bravo rail", "charlie hatch"]
    states = [{}, STATE, {**STATE, "next_actions": STEPS}]
    for name, state in zip(names, states, strict=True):
        task_id = make_task(dsn, name)
        if state:
            with db.transaction(dsn) as cur:
                update_task(cur, task_id, **state)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    rows = work_ui._load(dsn, "active")
    frames = [
        work_ui._screen_text(rows, at, "active", total=len(rows), query="")
        for at in range(len(rows))
    ]
    assert_steady_layout(frames, names, work_ui._TASK_KEYS)


def test_the_last_page_of_a_detail_is_as_tall_as_the_first(monkeypatch):
    monkeypatch.setenv("LINES", "24")
    text = "title\n\n\n" + "\n".join(f"line {n}" for n in range(30))
    first, more = screen.paged(text, 0, work_ui._DETAIL_KEYS)
    last, end = screen.paged(text, more, work_ui._DETAIL_KEYS)
    assert more and not end
    assert len(last.split("\n")) == len(first.split("\n"))
