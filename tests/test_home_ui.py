from __future__ import annotations

import io
import sys

import pytest

from conftest import candidate, expire, new_project, new_task, remember, retire
from mashu import cli, db, home_ui, nominations, routing, scopes, screen, tasks, temporary


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeReadline:
    def __init__(self) -> None:
        self.startup_hook = None
        self.inserted = None
        self.redisplays = 0

    def set_startup_hook(self, hook=None) -> None:
        self.startup_hook = hook

    def insert_text(self, value: str) -> None:
        self.inserted = value

    def redisplay(self) -> None:
        self.redisplays += 1


def test_editline_starts_with_the_existing_value(monkeypatch):
    readline = FakeReadline()
    monkeypatch.setitem(sys.modules, "readline", readline)
    monkeypatch.setattr(sys, "stdin", TtyBuffer())

    def edit(_prompt: str) -> str:
        assert readline.startup_hook is not None
        readline.startup_hook()
        return "edited value"

    monkeypatch.setattr("builtins.input", edit)

    assert screen.editline("edit: ", "existing value") == screen.Submitted("edited value")
    assert readline.inserted == "existing value"
    assert readline.redisplays == 0
    assert readline.startup_hook is None


def test_editline_distinguishes_an_empty_submission_from_cancellation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert screen.editline("edit: ", "existing") == screen.Submitted("")

    def cancel(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)
    assert screen.editline("edit: ", "existing") == screen.Cancelled()


def test_edit_text_shows_the_body_only_in_the_edit_buffer(monkeypatch):
    painted: list[str] = []
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(screen, "paint", painted.append)
    monkeypatch.setattr(
        screen,
        "editline",
        lambda prompt, current: opened.append((prompt, current)) or screen.Submitted("revised"),
    )

    assert screen.edit_text("Edit memory", "the existing body") == screen.Submitted("revised")
    assert "the existing body" not in painted[0]
    assert opened == [("  > ", "the existing body")]


def test_no_command_opens_the_home_screen(monkeypatch):
    called: list[str | None] = []
    monkeypatch.setattr(home_ui, "run", lambda dsn=None: called.append(dsn) or 0)

    assert cli.main(["--dsn", "dbname=somewhere"]) == 0
    assert called == ["dbname=somewhere"]


def test_dashboard_collects_review_task_memory_and_project_counts(dsn):
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="home scope", actor="user")
        routing.add_route(cur, path_prefix="/tmp/home", scope_id=scope["scope_id"], actor="user")
        new_project(cur, "home dashboard")
        remember(cur, "keep the dashboard count distinct")
        retire(cur, remember(cur, "retire this dashboard-only rule"), "a retired count")
        temporary.put_temporary(cur, content="the dashboard test is running", actor="user", days=1)
        candidate(cur, "show one ready dashboard candidate")
        deferred = candidate(cur, "show one deferred dashboard candidate", kind="incident")
        nominations.defer(cur, deferred["nomination_id"], actor="user", reason="wait for it")
        active, dormant, closed = (
            new_task(cur, f"{activity} dashboard task", "home dashboard", force=True)["task"][
                "task_id"
            ]
            for activity in ("active", "dormant", "closed")
        )
        tasks.propose_close(cur, active, outcome="completed", reason="it shows", actor="agent")
        expire(cur, dormant)
        tasks.close(cur, closed, outcome="completed", actor="user", reason="counted")

    assert home_ui._dashboard(dsn) == home_ui.Dashboard(
        review_ready=1,
        review_deferred=1,
        tasks_active=1,
        tasks_dormant=1,
        task_proposals=1,
        active_memories=1,
        projects=1,
        tasks_closed=1,
        retired_memories=1,
        temporary_contexts=1,
        scopes=1,
        routes=1,
        schema_pending=0,
    )


@pytest.mark.parametrize(
    ("pressed", "dashboard", "opened"),
    [
        (("r", "t", "q"), (0,) * 7, [("review", False), ("tasks", None)]),
        (("down", "enter", "q"), (0,) * 7, [("memories", None)]),
        (
            ("m", "w", "s", "q"),
            (0,) * 7,
            [("memories", None), ("work", "active"), ("settings", None)],
        ),
        (
            ("a", "down", "enter", "end", "enter", "q", "q"),
            (1, 2, 3, 4, 1, 5, 6),
            [("review", True), ("work", "dormant")],
        ),
        (("d", "q"), (0, 2, 0, 0, 0, 0, 0), [("review", True)]),
    ],
)
def test_home_keys_open_each_area_and_return_to_a_refreshed_dashboard(
    monkeypatch, pressed, dashboard, opened
):
    keys = iter(pressed)
    reads: list[str] = []
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(
        home_ui, "_dashboard", lambda dsn: reads.append(dsn) or home_ui.Dashboard(*dashboard)
    )
    monkeypatch.setattr(screen, "paint", lambda text: None)
    monkeypatch.setattr(screen, "getkey", lambda: next(keys))

    def opener(area):
        def run(dsn, *, show_deferred=False, initial_view="active"):
            assert dsn == "home-dsn"
            option = {"review": show_deferred, "work": initial_view}.get(area)
            seen.append((area, option))
            return 0

        return run

    for area, module in (
        ("review", home_ui.review_ui),
        ("tasks", home_ui.close_ui),
        ("memories", home_ui.memory_ui),
        ("work", home_ui.work_ui),
        ("settings", home_ui.settings_ui),
    ):
        monkeypatch.setattr(module, "run", opener(area))

    assert home_ui.run("home-dsn") == 0
    assert seen == opened
    assert set(reads) == {"home-dsn"} and len(reads) > len(opened)


def test_non_tty_dashboard_contains_no_ansi(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    state = home_ui.Dashboard(1, 2, 3, 4, 1, 5, 6)

    rendered = home_ui._screen_text(state, 0)

    assert "\x1b[" not in rendered
    assert "Attention" in rendered and "Memories" in rendered
    assert "Work" in rendered and "Settings & health" in rendered
    assert "1 ready" in rendered and "2 deferred" in rendered
    assert "3 active" in rendered and "4 dormant" in rendered


def test_terminal_session_enters_lazily_once_when_nested_and_restores_after_an_error(monkeypatch):
    terminal = TtyBuffer()
    monkeypatch.setattr("sys.stdout", terminal)

    with screen.terminal_session():
        print("empty queue")
    assert terminal.getvalue() == "empty queue\n"

    with screen.terminal_session():
        screen.paint("dashboard")
        with screen.terminal_session():
            screen.paint("review")
    output = terminal.getvalue()
    assert output.count("\x1b[?1049h") == 1
    assert output.count("\x1b[?1049l") == 1
    assert "dashboard" in output and "review" in output

    with pytest.raises(RuntimeError, match="boom"):
        with screen.terminal_session():
            screen.paint("started")
            raise RuntimeError("boom")
    assert terminal.getvalue().endswith("\x1b[?1049l")
