from __future__ import annotations

import io

import psycopg
import pytest

from mashu import (
    cli,
    db,
    home_ui,
    ledger,
    memories,
    nominations,
    projects,
    routing,
    scopes,
    screen,
    tasks,
    temporary,
)
from mashu.migrate import migrate

ADMIN_DSN = "dbname=postgres"
TEST_DB = "mashu_test_home"


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def home_dsn() -> str:
    """A clean store whose aggregate counts are exact."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    dsn = f"dbname={TEST_DB}"
    migrate(dsn)
    return dsn


def test_no_command_opens_the_home_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str | None] = []
    monkeypatch.setattr(home_ui, "run", lambda dsn=None: called.append(dsn) or 0)

    assert cli.main(["--dsn", "dbname=somewhere"]) == 0
    assert called == ["dbname=somewhere"]


def test_dashboard_collects_review_task_memory_and_project_counts(
    home_dsn: str,
) -> None:
    with db.transaction(home_dsn) as cur:
        scope = scopes.create_scope(
            cur, name="home scope", summary="dashboard test scope", actor="user"
        )
        routing.add_route(
            cur, path_prefix="/tmp/mashu-home-dashboard", scope_id=scope["scope_id"], actor="user"
        )
        projects.create_project(cur, name="home dashboard", actor="user")
        memories.remember(cur, content="keep the dashboard count distinct", actor="user")
        retired = memories.remember(cur, content="retire this dashboard-only rule", actor="user")
        memories.retire(
            cur,
            retired["memory_id"],
            reason="the dashboard test needs a retired count",
            actor="user",
        )
        temporary.put_temporary(
            cur,
            content="the dashboard test is running today",
            actor="user",
            days=1,
        )
        pain = ledger.report_pain(
            cur,
            kind="friction",
            what="the home count was absent",
            prevention="show one ready dashboard candidate",
            actor="agent",
        )
        nominations.create_nomination(
            cur,
            content="show one ready dashboard candidate",
            kind="rederivation",
            evidence=[pain["ledger_id"]],
            actor="agent",
        )
        deferred_pain = ledger.report_pain(
            cur,
            kind="incident",
            what="a deferred count disappeared",
            prevention="show one deferred dashboard candidate",
            actor="agent",
        )
        deferred = nominations.create_nomination(
            cur,
            content="show one deferred dashboard candidate",
            kind="incident",
            evidence=[deferred_pain["ledger_id"]],
            actor="agent",
        )
        nominations.defer(
            cur,
            deferred["nomination_id"],
            actor="user",
            reason="wait for the dashboard test",
        )
        active = tasks.task_create(
            cur,
            project="home dashboard",
            name="active dashboard task",
            goal="be counted as active",
            actor="agent",
        )
        tasks.propose_close(
            cur,
            active["task"]["task_id"],
            outcome="completed",
            reason="the count is visible",
            actor="agent",
        )
        dormant = tasks.task_create(
            cur,
            project="home dashboard",
            name="dormant dashboard task",
            goal="be counted as dormant",
            actor="agent",
            force=True,
        )
        cur.execute(
            "UPDATE task SET active_until = now() - interval '1 day' WHERE task_id = %s",
            (dormant["task"]["task_id"],),
        )
        closed = tasks.task_create(
            cur,
            project="home dashboard",
            name="closed dashboard task",
            goal="be counted as closed",
            actor="agent",
            force=True,
        )
        tasks.close(
            cur,
            closed["task"]["task_id"],
            outcome="completed",
            actor="user",
            reason="counted by the dashboard test",
        )

    state = home_ui._dashboard(home_dsn)

    assert state == home_ui.Dashboard(
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


def test_review_and_close_screens_return_to_a_refreshed_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = iter(("r", "t", "q"))
    reads: list[str | None] = []
    opened: list[tuple[str, str | None]] = []
    empty = home_ui.Dashboard(0, 0, 0, 0, 0, 0, 0)
    monkeypatch.setattr(home_ui, "_dashboard", lambda dsn: reads.append(dsn) or empty)
    monkeypatch.setattr(screen, "paint", lambda text: None)
    monkeypatch.setattr(screen, "getkey", lambda: next(keys))
    monkeypatch.setattr(home_ui.review_ui, "run", lambda dsn: opened.append(("review", dsn)) or 0)
    monkeypatch.setattr(home_ui.close_ui, "run", lambda dsn: opened.append(("tasks", dsn)) or 0)

    assert home_ui.run("home-dsn") == 0
    assert opened == [("review", "home-dsn"), ("tasks", "home-dsn")]
    assert reads == ["home-dsn", "home-dsn", "home-dsn"]


def test_arrows_and_enter_open_the_selected_area(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = iter(("down", "enter", "q"))
    opened: list[str] = []
    empty = home_ui.Dashboard(0, 0, 0, 0, 0, 0, 0)
    monkeypatch.setattr(home_ui, "_dashboard", lambda dsn: empty)
    monkeypatch.setattr(screen, "paint", lambda text: None)
    monkeypatch.setattr(screen, "getkey", lambda: next(keys))
    monkeypatch.setattr(home_ui.memory_ui, "run", lambda dsn: opened.append("memories") or 0)

    assert home_ui.run() == 0
    assert opened == ["memories"]


def test_home_shortcuts_open_memory_work_and_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = iter(("m", "w", "s", "q"))
    opened: list[str] = []
    empty = home_ui.Dashboard(0, 0, 0, 0, 0, 0, 0)
    monkeypatch.setattr(home_ui, "_dashboard", lambda dsn: empty)
    monkeypatch.setattr(screen, "paint", lambda text: None)
    monkeypatch.setattr(screen, "getkey", lambda: next(keys))
    monkeypatch.setattr(home_ui.memory_ui, "run", lambda dsn: opened.append("memories") or 0)
    monkeypatch.setattr(home_ui.work_ui, "run", lambda dsn: opened.append("work") or 0)
    monkeypatch.setattr(home_ui.settings_ui, "run", lambda dsn: opened.append("settings") or 0)

    assert home_ui.run("home-dsn") == 0
    assert opened == ["memories", "work", "settings"]


def test_attention_menu_opens_deferred_and_dormant_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = iter(("a", "down", "enter", "end", "enter", "q", "q"))
    opened: list[tuple[str, object]] = []
    empty = home_ui.Dashboard(1, 2, 3, 4, 1, 5, 6)
    monkeypatch.setattr(home_ui, "_dashboard", lambda dsn: empty)
    monkeypatch.setattr(screen, "paint", lambda text: None)
    monkeypatch.setattr(screen, "getkey", lambda: next(keys))
    monkeypatch.setattr(
        home_ui.review_ui,
        "run",
        lambda dsn, *, show_deferred=False: opened.append(("review", show_deferred)) or 0,
    )
    monkeypatch.setattr(
        home_ui.work_ui,
        "run",
        lambda dsn, *, initial_view="active": opened.append(("work", initial_view)) or 0,
    )

    assert home_ui.run("home-dsn") == 0
    assert opened == [("review", True), ("work", "dormant")]


def test_deferred_shortcut_opens_the_complete_review_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = iter(("d", "q"))
    opened: list[bool] = []
    empty = home_ui.Dashboard(0, 2, 0, 0, 0, 0, 0)
    monkeypatch.setattr(home_ui, "_dashboard", lambda dsn: empty)
    monkeypatch.setattr(screen, "paint", lambda text: None)
    monkeypatch.setattr(screen, "getkey", lambda: next(keys))
    monkeypatch.setattr(
        home_ui.review_ui,
        "run",
        lambda dsn, *, show_deferred=False: opened.append(show_deferred) or 0,
    )

    assert home_ui.run() == 0
    assert opened == [True]


def test_non_tty_dashboard_contains_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    state = home_ui.Dashboard(1, 2, 3, 4, 1, 5, 6)

    rendered = home_ui._screen_text(state, 0)

    assert "\x1b[" not in rendered
    assert "Attention" in rendered and "Memories" in rendered
    assert "Work" in rendered and "Settings & health" in rendered
    assert "1 ready" in rendered and "2 deferred" in rendered
    assert "3 active" in rendered and "4 dormant" in rendered


def test_nested_terminal_sessions_use_one_discarded_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = TtyBuffer()
    monkeypatch.setattr("sys.stdout", terminal)

    with screen.terminal_session():
        screen.paint("dashboard")
        with screen.terminal_session():
            screen.paint("review")

    output = terminal.getvalue()
    assert output.count("\x1b[?1049h") == 1
    assert output.count("\x1b[?1049l") == 1
    assert "dashboard" in output and "review" in output


def test_terminal_session_enters_lazily_and_restores_after_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = TtyBuffer()
    monkeypatch.setattr("sys.stdout", terminal)

    with screen.terminal_session():
        print("empty queue")
    assert terminal.getvalue() == "empty queue\n"

    with pytest.raises(RuntimeError, match="boom"):
        with screen.terminal_session():
            screen.paint("started")
            raise RuntimeError("boom")
    assert terminal.getvalue().endswith("\x1b[?1049l")
