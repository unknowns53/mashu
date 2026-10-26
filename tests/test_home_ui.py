from __future__ import annotations

import io

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


def test_editline_distinguishes_an_empty_submission_from_cancellation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert screen.editline("edit: ", "existing") == screen.Submitted("")

    def cancel(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)
    assert screen.editline("edit: ", "existing") == screen.Cancelled()


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
