from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from mashu import (
    db,
    ledger,
    memories,
    nominations,
    projects,
    routing,
    scopes,
    screen,
    settings_ui,
    tasks,
    temporary,
)
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_settings"


@pytest.fixture
def dsn() -> Iterator[str]:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    conninfo = f"dbname={TEST_DB}"
    migrate(conninfo)
    yield conninfo


@pytest.fixture(autouse=True)
def known_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "30")


def keys(monkeypatch: pytest.MonkeyPatch, *pressed: str) -> None:
    values = iter(pressed)
    monkeypatch.setattr(screen, "getkey", lambda: next(values))


def answers(monkeypatch: pytest.MonkeyPatch, *typed: str) -> list[str]:
    values = iter(typed)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr("builtins.input", answer)
    return prompts


def test_top_level_reaches_every_settings_page_and_leaves_cleanly(
    dsn: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keys(
        monkeypatch,
        "enter",
        "q",
        "down",
        "enter",
        "q",
        "down",
        "enter",
        "q",
        "down",
        "enter",
        "q",
        "down",
        "enter",
        "q",
        "q",
    )

    assert settings_ui.run(dsn) == 0

    out = capsys.readouterr().out
    for heading in (
        "Health/status",
        "Bootstrap preview",
        "Scopes",
        "Routes",
        "Schema/migrations",
    ):
        assert heading in out
    assert "\x1b[" not in out
    assert hasattr(settings_ui.run, "__wrapped__")


def test_health_collects_capacity_queues_evidence_and_task_activity(dsn: str) -> None:
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="health", summary="health checks", actor="user")
        memories.remember(cur, content="global health memory", actor="user")
        memories.remember(
            cur,
            content="scoped health memory",
            scope_id=scope["scope_id"],
            delivery="scope",
            actor="user",
        )
        pain = ledger.report_pain(
            cur,
            kind="incident",
            what="the health queue vanished",
            prevention="show the health queue",
            actor="agent",
        )
        nominations.create_nomination(
            cur,
            content="show the health queue",
            kind="incident",
            evidence=[pain["ledger_id"]],
            actor="agent",
        )
        deferred_pain = ledger.report_pain(
            cur,
            kind="friction",
            what="the deferred health count vanished",
            prevention="show the deferred health queue",
            actor="agent",
        )
        deferred = nominations.create_nomination(
            cur,
            content="show the deferred health queue",
            kind="rederivation",
            evidence=[deferred_pain["ledger_id"]],
            actor="agent",
        )
        nominations.defer(
            cur,
            deferred["nomination_id"],
            actor="user",
            reason="exercise the deferred count",
        )
        temporary.put_temporary(
            cur,
            content="temporary health condition",
            actor="user",
            days=1,
        )
        projects.create_project(cur, name="health project", actor="user")
        tasks.task_create(
            cur,
            project="health project",
            name="active health task",
            goal="appear in active health",
            actor="agent",
        )
        dormant = tasks.task_create(
            cur,
            project="health project",
            name="dormant health task",
            goal="appear in dormant health",
            actor="agent",
            force=True,
        )
        closed = tasks.task_create(
            cur,
            project="health project",
            name="closed health task",
            goal="appear in closed health",
            actor="agent",
            force=True,
        )
        cur.execute(
            "UPDATE task SET active_until = now() - interval '1 day' WHERE task_id = %s",
            (dormant["task"]["task_id"],),
        )
        tasks.close(
            cur,
            closed["task"]["task_id"],
            outcome="completed",
            actor="user",
            reason="health test finished",
        )
        cur.execute(
            """
            INSERT INTO trace (content, created_by, expires_at)
            VALUES ('live health trace', 'agent', now() + interval '1 day')
            """
        )
        cur.execute(
            """
            INSERT INTO event_log (event_type, actor)
            VALUES ('delivery_failure_suspected', 'agent')
            """
        )

    value = settings_ui._health(dsn)

    assert value.schema_pending == ()
    assert value.memory_counts == {"always": 1, "scope": 1}
    assert value.memory_always_tokens > 0 and value.memory_worst_tokens > 0
    assert value.active_states == 1 and value.state_worst_tokens > 0
    assert value.temporary_count == 1 and value.temporary_tokens > 0
    # Incident reporting nominates immediately; the explicit second row makes two ready.
    assert (value.pending_ready, value.pending_deferred) == (2, 1)
    assert value.traces == 1 and value.ledger_30d >= 4
    assert value.delivery_failures_30d == 1 and value.scope_count == 1
    assert (value.tasks_active, value.tasks_dormant, value.tasks_closed) == (1, 1, 1)


def test_bootstrap_preview_resolves_cwd_and_shows_every_payload_share(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = "/tmp/mashu-settings-project"
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="preview", summary="preview scope", actor="user")
        routing.add_route(cur, path_prefix=here, scope_id=scope["scope_id"], actor="user")
        memories.remember(cur, content="always in preview", actor="user")
        memories.remember(
            cur,
            content="scoped into preview",
            scope_id=scope["scope_id"],
            delivery="scope",
            actor="user",
        )
        projects.create_project(
            cur,
            name="preview project",
            scope_id=scope["scope_id"],
            actor="user",
        )
        tasks.task_create(
            cur,
            project="preview project",
            name="preview active state",
            goal="show the current state",
            actor="agent",
        )
        temporary.put_temporary(
            cur,
            content="preview temporary context",
            scope_id=scope["scope_id"],
            actor="user",
            days=1,
        )
    monkeypatch.setattr(settings_ui.os, "getcwd", lambda: f"{here}/nested")

    answer = settings_ui._bootstrap_preview(dsn)
    rendered = settings_ui._bootstrap_text(answer)

    assert answer["scope"] == "preview" and answer["routed"] is True
    assert answer["always"][0]["content"] == "always in preview"
    assert answer["scoped"][0]["content"] == "scoped into preview"
    assert answer["states"][0]["content"].startswith("preview active state")
    assert answer["temporary"][0]["content"] == "preview temporary context"
    assert "Tokens" in rendered and "Total" in rendered


def test_scope_page_creates_a_scope_with_its_summary(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys(monkeypatch, "n", "q")
    answers(monkeypatch, "deployment", "Release and rollback knowledge")

    settings_ui._scopes_page(dsn)

    with db.transaction(dsn) as cur:
        row = scopes.require_scope(cur, "deployment")
    assert row["summary"] == "Release and rollback knowledge"


def test_scope_errors_stay_on_the_page_as_feedback(
    dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="duplicate", actor="user")
    keys(monkeypatch, "n", "q")
    answers(monkeypatch, "duplicate", "another summary")

    settings_ui._scopes_page(dsn)

    assert "already exists" in capsys.readouterr().out


def test_routes_can_be_added_ignored_and_removed(dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="route scope", actor="user")
    keys(monkeypatch, "n", "i", "x", "q")
    answers(
        monkeypatch,
        "/tmp/settings-scoped/a/long/path",
        "route scope",
        "/tmp/i",
        "y",
    )

    settings_ui._routes_page(dsn)

    rows = settings_ui._route_rows(dsn)
    assert [(row["path_prefix"], row["scope_id"]) for row in rows] == [("/tmp/i", None)]


def test_schema_apply_is_confirmed_and_reports_a_noop(
    dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[str | None] = []
    keys(monkeypatch, "m", "q")
    prompts = answers(monkeypatch, "y")
    monkeypatch.setattr(
        settings_ui.migration,
        "migrate",
        lambda conninfo: called.append(conninfo) or [],
    )

    settings_ui._schema_page(dsn)

    assert called == [dsn]
    assert prompts and "apply 0 pending migration" in prompts[0]
    assert "already up to date" in capsys.readouterr().out


def test_long_pages_move_forward_and_back_without_losing_the_header(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LINES", "10")
    keys(monkeypatch, "space", "b", "q")
    body = "\n".join(f"  detail line {number}" for number in range(30))

    settings_ui._read_page("Long health page", body)

    out = capsys.readouterr().out
    assert "more line(s)" in out
    assert out.count("Long health page") == 3
    assert out.count("detail line 0") == 2
