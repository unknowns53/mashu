from __future__ import annotations

import re
from uuid import uuid4

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
from mashu import close_ui, db, screen, task_actions
from mashu.errors import MashuError

MERGED = "deleted before the merge d178ecb7, nothing on main references it"
DETAIL = {
    "goal": "launch without the old rail",
    "status_text": "the replacement is fitted",
    "approach": "reuse the aft mounting points",
    "open_questions": ["whether the forward mount also moves"],
    "blockers": ["await the load certificate"],
    "next_actions": ["run the loaded trial"],
}

STEPS = [f"step {n} of the loaded trial and its checks" for n in range(5)]

pytestmark = pytest.mark.usefixtures("known_screen")


@pytest.fixture
def dsn(dsn):
    with db.transaction(dsn) as cur:
        new_project(cur)
    return dsn


def a_task(dsn: str, name: str, *, propose: str | None = None, dormant=False, **state):
    task_id = committed_task(dsn, name, propose=propose, reason=MERGED)
    with db.transaction(dsn) as cur:
        if state:
            update_task(cur, task_id, **state)
        if dormant:
            expire(cur, task_id)
    return task_id


def status(dsn, task_id):
    return task_row(dsn, task_id)["task"]["status"]


@pytest.mark.parametrize(("key", "proposal"), [("w", None), ("t", "completed")])
def test_drop_and_renew_leave_the_task_open_with_its_lease_renewed(dsn, monkeypatch, key, proposal):
    task_id = a_task(dsn, "drop the close-up tool", propose="completed", dormant=True)
    keys(monkeypatch, key, "q")

    assert close_ui.run(dsn) == 0

    row = task_row(dsn, task_id)
    assert (row["task"]["status"], row["activity"]) == ("open", "active")
    assert (row["proposal"] and row["proposal"]["outcome"]) == proposal


def test_proposals_lead_dormant_ones_included_and_arrows_reach_the_rest(dsn, monkeypatch):
    unproposed = a_task(dsn, "rework the hull")
    proposed = a_task(dsn, "drop the close-up tool", propose="completed", dormant=True)
    keys(monkeypatch, "enter", "q")
    assert close_ui.run(dsn) == 0
    assert status(dsn, proposed) == "closed"

    a_task(dsn, "inspect the deck", propose="completed")
    keys(monkeypatch, "down", "c", "", "q")
    assert close_ui.run(dsn) == 0
    assert task_row(dsn, unproposed)["task"]["outcome"] == "completed"


def test_a_store_with_nothing_open_says_so_rather_than_painting_a_list(dsn, monkeypatch, capsys):
    keys(monkeypatch, "q")
    assert close_ui.run(dsn) == 0
    assert "no open tasks" in capsys.readouterr().out


def test_preview_shows_grounds_and_context_and_leaving_decides_nothing(dsn, monkeypatch, capsys):
    task_id = a_task(dsn, "replace the launch rail", propose="completed", **DETAIL)
    keys(monkeypatch, "q")

    assert close_ui.run(dsn) == 0
    assert status(dsn, task_id) == "open"
    out = capsys.readouterr().out
    assert MERGED in out and "PROPOSAL" in out and "STALE" in out
    for value in DETAIL.values():
        assert (value[0] if isinstance(value, list) else value) in out
    for label in ("goal", "status", "approach", "open questions", "blockers", "next actions"):
        assert label in out


@pytest.mark.parametrize(
    "query", ["REPLACE", "ENRAI", "ACTIVE", "LAUNCH WITHOUT", "FITTED", "FORWARD MOUNT", "ON MAIN"]
)
def test_search_matches_every_promised_text_field_case_insensitively(dsn, query):
    task_id = a_task(dsn, "replace the launch rail", propose="completed", **DETAIL)
    row = task_row(dsn, task_id)

    assert close_ui._matches(row, query)
    assert close_ui._matches(row, str(task_id)[:12])


@pytest.mark.parametrize(
    "searches",
    [
        ("/", "HULL", "/", "", "home"),
        ("/", "hull", "left", "home"),
        ("/", "absent", "/", "", "home"),
    ],
)
def test_a_search_narrows_the_list_and_clearing_it_restores_the_whole_list(
    dsn, monkeypatch, searches
):
    hull = a_task(dsn, "rework the hull")
    deck = a_task(dsn, "inspect the deck")
    keys(monkeypatch, *searches, "c", "", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, deck)["task"]["outcome"] == "completed"
    assert status(dsn, hull) == "open"


@pytest.mark.parametrize(("movement", "closed_index"), [(("j",), 0), (("end", "k"), 1)])
def test_jk_and_home_end_move_between_tasks(dsn, monkeypatch, movement, closed_index):
    task_ids = [a_task(dsn, "older task"), a_task(dsn, "newer task")]
    keys(monkeypatch, *movement, "c", "", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, task_ids[closed_index])["task"]["outcome"] == "completed"
    assert status(dsn, task_ids[1 - closed_index]) == "open"


def test_page_down_moves_by_more_than_one_visible_task(dsn, monkeypatch):
    task_ids = [a_task(dsn, f"task {number:02}") for number in range(15)]
    keys(monkeypatch, "pagedown", "c", "", "q")

    assert close_ui.run(dsn) == 0
    closed = [task_id for task_id in task_ids if status(dsn, task_id) == "closed"]
    assert len(closed) == 1
    assert status(dsn, task_ids[-1]) == "open"
    assert status(dsn, task_ids[-2]) == "open"


def test_selection_survives_reload_when_withdrawing_reorders_the_list(dsn, monkeypatch):
    selected = a_task(dsn, "selected proposal", propose="completed")
    other = a_task(dsn, "other proposal", propose="completed")
    keys(monkeypatch, "down", "w", "c", "selected remained selected", "q")

    assert close_ui.run(dsn) == 0
    assert task_row(dsn, selected)["task"]["outcome"] == "completed"
    assert status(dsn, other) == "open"


def test_non_tty_output_has_no_ansi_and_small_screens_stay_bounded(dsn, monkeypatch, capsys):
    a_task(dsn, "replace the launch rail", propose="completed", **DETAIL)
    monkeypatch.setenv("LINES", "12")
    keys(monkeypatch, "q")

    assert close_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    assert len(out.splitlines()) <= 12


def test_every_preview_label_stands_two_spaces_clear_of_its_value(dsn):
    task_id = a_task(dsn, "replace the launch rail", propose="completed", **DETAIL)

    detail = close_ui._detail(task_row(dsn, task_id))

    assert re.search(r"open questions {2,}whether the forward mount", detail)
    assert re.search(r"reason {2,}deleted before the merge", detail)


def test_the_project_column_fits_the_longest_name_and_activity_is_spelled_out():
    rows = [
        {
            "task": {"task_id": uuid4(), "project_name": name, "name": "a task"},
            "activity": activity,
            "age_days": 3,
            "heading": "",
            "state": {},
        }
        for name, activity in (("mythogenesis", "active"), ("output-style", "dormant"))
    ]

    text = close_ui._screen_text(rows, 0)

    assert text.startswith("Close tasks\n")
    assert "mythogenesis  active" in text and "output-style  dormant" in text
    assert "…" not in text


def test_moving_the_cursor_moves_neither_the_list_nor_the_keys(dsn, monkeypatch):
    names = ["alpha rail", "bravo hatch", "charlie mount", "delta keel"]
    a_task(dsn, names[0])
    a_task(dsn, names[1], propose="completed", **DETAIL)
    a_task(dsn, names[2], goal="fit the mount")
    a_task(dsn, names[3], propose="abandoned", next_actions=STEPS)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    rows = close_ui._open_tasks(dsn, None)
    frames = [close_ui._screen_text(rows, at, total=len(rows)) for at in range(len(rows))]
    assert_steady_layout(frames, names, close_ui._KEYS)


def test_y_takes_only_standing_proposals_on_their_outcome_and_grounds(dsn, monkeypatch, capsys):
    done = a_task(dsn, "fit the rail", propose="completed")
    dropped = a_task(dsn, "drop the hatch", propose="abandoned")
    stale = a_task(dsn, "move the keel", propose="completed", goal="one more check")
    bare = a_task(dsn, "paint the hull")
    keys(monkeypatch, "y", "y", "q")

    assert close_ui.run(dsn) == 0

    for task_id, outcome in ((done, "completed"), (dropped, "abandoned")):
        closed = task_row(dsn, task_id)["task"]
        assert (closed["status"], closed["outcome"], closed["close_reason"]) == (
            "closed",
            outcome,
            MERGED,
        )
    assert task_row(dsn, stale)["proposal"]["stale"]
    assert (status(dsn, stale), status(dsn, bare)) == ("open", "open")
    out = capsys.readouterr().out
    assert "close 2 task(s)" in out and "move the keel" not in out.split("close 2 task(s)")[1][:200]
    assert "closed 2 task(s)" in out and "1 stale proposal(s) left for review" in out


def test_anything_but_y_at_the_confirmation_closes_nothing(dsn, monkeypatch):
    task_ids = [a_task(dsn, name, propose="completed") for name in ("fit the rail", "drop it")]
    keys(monkeypatch, "y", "n", "q")

    assert close_ui.run(dsn) == 0
    for task_id in task_ids:
        row = task_row(dsn, task_id)
        assert row["task"]["status"] == "open" and row["proposal"]["reason"] == MERGED


def test_a_failed_close_stops_y_and_keeps_the_ones_already_closed(dsn, monkeypatch, capsys):
    task_ids = [a_task(dsn, f"task {n}", propose="completed") for n in range(3)]
    accept = task_actions.accept_close_proposal
    calls = []

    def second_fails(cur, task_id, **kw):
        calls.append((task_id, kw["actor"]))
        if len(calls) == 2:
            raise MashuError("the task or close proposal changed")
        return accept(cur, task_id, **kw)

    monkeypatch.setattr(task_actions, "accept_close_proposal", second_fails)
    keys(monkeypatch, "y", "y", "q")

    assert close_ui.run(dsn) == 0
    assert [actor for _, actor in calls] == ["user", "user"]
    assert [status(dsn, task_id) for task_id, _ in calls] == ["closed", "open"]
    assert sum(status(dsn, task_id) == "open" for task_id in task_ids) == 2
    failed = str(calls[1][0])[:8]
    assert f"{failed} not closed: the task or close proposal changed" in capsys.readouterr().out


def test_the_keys_fit_80_columns_and_a_long_confirmation_fits_the_screen(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    for keys_text in (close_ui._KEYS, close_ui._ALL_KEYS):
        assert all(screen.cells(line) <= 80 for line in keys_text.splitlines())
    rows = [
        {
            "task": {"task_id": uuid4(), "name": f"task {n} " + "long name " * 9},
            "proposal": {"outcome": "completed", "stale": False},
        }
        for n in range(30)
    ]

    text = close_ui._all_screen(rows, 2)

    lines = text.splitlines()
    assert len(lines) == 23 and all(screen.cells(line) <= 80 for line in lines)
    assert lines[-1] == close_ui._ALL_KEYS
    assert f"… and {30 - (23 - 7)} more" in text
