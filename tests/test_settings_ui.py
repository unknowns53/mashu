from __future__ import annotations

import pytest

from conftest import candidate, expire, getkeys, new_project, new_task, remember
from mashu import db, nominations, routing, scopes, settings_ui, tasks, temporary, topics
from mashu.errors import RefusedError

pytestmark = pytest.mark.usefixtures("known_screen")


def answers(monkeypatch, *typed: str) -> list[str]:
    values = iter(typed)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(values)

    monkeypatch.setattr("builtins.input", answer)
    return prompts


def test_top_level_reaches_every_settings_page_and_leaves_cleanly(dsn, monkeypatch, capsys):
    getkeys(monkeypatch, "enter", "q", *(("down", "enter", "q") * 5), "q")

    assert settings_ui.run(dsn) == 0

    out = capsys.readouterr().out
    for heading in (
        "Health/status",
        "Bootstrap preview",
        "Scopes",
        "Topics",
        "Routes",
        "Schema/migrations",
    ):
        assert heading in out
    assert "\x1b[" not in out
    assert hasattr(settings_ui.run, "__wrapped__")


def test_health_collects_capacity_queues_evidence_and_task_activity(dsn):
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="health", summary="health checks", actor="user")
        remember(cur, "global health memory")
        remember(cur, "scoped health memory", scope_id=scope["scope_id"], delivery="scope")
        candidate(cur, "show the health queue", kind="incident")
        deferred = candidate(cur, "show the deferred health queue")
        nominations.defer(cur, deferred["nomination_id"], actor="user", reason="the count")
        temporary.put_temporary(cur, content="temporary health condition", actor="user", days=1)
        new_project(cur, "health project")
        active, dormant, closed = (
            new_task(cur, f"{activity} health task", "health project", force=True)["task"][
                "task_id"
            ]
            for activity in ("active", "dormant", "closed")
        )
        expire(cur, dormant)
        tasks.close(cur, closed, outcome="completed", actor="user", reason="finished")
        cur.execute(
            "INSERT INTO trace (content, created_by, expires_at) "
            "VALUES ('live health trace', 'agent', now() + interval '1 day')"
        )
        cur.execute(
            "INSERT INTO event_log (event_type, actor) "
            "VALUES ('delivery_failure_suspected', 'agent')"
        )

    value = settings_ui._health(dsn)

    assert value.schema_pending == ()
    assert value.memory_counts == {"always": 1, "scope": 1}
    assert value.memory_always_tokens > 0 and value.memory_worst_tokens > 0
    assert value.active_states == 1 and value.state_worst_tokens > 0
    assert value.temporary_count == 1 and value.temporary_tokens > 0
    assert (value.pending_ready, value.pending_deferred) == (1, 1)
    assert value.traces == 1 and value.ledger_30d >= 4
    assert value.delivery_failures_30d == 1 and value.scope_count == 1
    assert (value.tasks_active, value.tasks_dormant, value.tasks_closed) == (1, 1, 1)


def test_bootstrap_preview_resolves_cwd_and_shows_every_payload_share(dsn, monkeypatch):
    here = "/tmp/mashu-settings-project"
    with db.transaction(dsn) as cur:
        scope_id = scopes.create_scope(cur, name="preview", actor="user")["scope_id"]
        routing.add_route(cur, path_prefix=here, scope_id=scope_id, actor="user")
        remember(cur, "always in preview")
        remember(cur, "scoped into preview", scope_id=scope_id, delivery="scope")
        new_project(cur, "preview project", scope_id=scope_id)
        new_task(cur, "preview active state", "preview project", goal="show the current state")
        temporary.put_temporary(
            cur, content="preview temporary context", scope_id=scope_id, actor="user", days=1
        )
    monkeypatch.setattr(settings_ui.os, "getcwd", lambda: f"{here}/nested")

    answer = settings_ui._bootstrap_preview(dsn)
    rendered = settings_ui._bootstrap_text(answer)

    assert answer["scope"] == "preview" and answer["routed"] is True
    assert answer["always"][0]["content"] == "always in preview"
    assert answer["scoped"][0]["content"] == "scoped into preview"
    assert answer["states"][0]["content"].startswith("preview active state")
    assert "call task_get" in answer["task_instruction"]
    assert answer["temporary"][0]["content"] == "preview temporary context"
    assert "Active task cards" in rendered
    assert "call task_get" in rendered
    assert "Tokens" in rendered and "Total" in rendered


@pytest.mark.parametrize("summary", ["Release and rollback knowledge", ""])
def test_scope_page_summary_is_optional_and_a_duplicate_refused(dsn, monkeypatch, capsys, summary):
    getkeys(monkeypatch, "n", "n", "q")
    answers(monkeypatch, "deployment", summary, "deployment", "another summary")

    settings_ui._scopes_page(dsn)

    assert "already exists" in capsys.readouterr().out
    with db.transaction(dsn) as cur:
        row = scopes.require_scope(cur, "deployment")
    assert row["summary"] == (summary or None)


def test_ctrl_c_at_an_optional_scope_summary_cancels_creation(dsn, monkeypatch):
    getkeys(monkeypatch, "n", "q")

    def answer(prompt: str) -> str:
        if prompt == "  scope name: ":
            return "deployment"
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", answer)
    settings_ui._scopes_page(dsn)

    with db.transaction(dsn) as cur:
        assert scopes.get_scope(cur, "deployment") is None


def test_scope_name_and_summary_can_be_edited(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="old scope", summary="old summary", actor="user")
    getkeys(monkeypatch, "e", "q")
    answers(monkeypatch, "new scope", "new summary")

    settings_ui._scopes_page(dsn)

    with db.transaction(dsn) as cur:
        row = scopes.require_scope(cur, "new scope")
    assert row["scope_id"] == scope["scope_id"]
    assert row["summary"] == "new summary"


def test_scope_refuses_banned_text_and_flags_unreadable_patterns(cur, tmp_path, monkeypatch):
    for field in ("name", "summary"):
        with pytest.raises(RefusedError):
            scopes.create_scope(
                cur, actor="user", **{"name": "clean", "summary": "clean", field: "SECRETMARKER9"}
            )
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path))
    assert scopes.create_scope(cur, name="clean", actor="user")["unchecked"] is True


def test_routes_can_be_edited_added_ignored_and_removed(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="route scope", actor="user")
        route = routing.add_route(
            cur, path_prefix="/tmp/old", scope_id=scope["scope_id"], actor="user"
        )
    getkeys(monkeypatch, "e", "n", "i", "x", "q")
    answers(monkeypatch, "/tmp/new", "-", "/tmp/scoped/a/long/path", "route scope", "/tmp/i", "y")

    settings_ui._routes_page(dsn)

    rows = settings_ui._route_rows(dsn)
    kept = [(row["path_prefix"], row["scope_id"]) for row in rows]
    assert kept == [("/tmp/new", None), ("/tmp/i", None)]
    assert rows[0]["route_id"] == route["route_id"]


def test_schema_apply_is_confirmed_and_reports_a_noop(dsn, monkeypatch, capsys):
    called: list[str | None] = []
    getkeys(monkeypatch, "m", "q")
    prompts = answers(monkeypatch, "y")
    monkeypatch.setattr(
        settings_ui.migration, "migrate", lambda conninfo: called.append(conninfo) or []
    )

    settings_ui._schema_page(dsn)

    assert called == [dsn]
    assert prompts and "apply 0 pending migration" in prompts[0]
    assert "already up to date" in capsys.readouterr().out


def test_long_pages_move_forward_and_back_without_losing_the_header(monkeypatch, capsys):
    monkeypatch.setenv("LINES", "10")
    getkeys(monkeypatch, "space", "b", "q")
    body = "\n".join(f"  detail line {number}" for number in range(30))

    settings_ui._read_page("Long health page", body)

    out = capsys.readouterr().out
    assert "more line(s)" in out
    assert out.count("Long health page") == 3
    assert out.count("detail line 0") == 2


def test_topic_page_creates_edits_and_removes_a_topic(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="game", actor="user")
    getkeys(monkeypatch, "n", "e", "x", "q")
    answers(
        monkeypatch,
        "difficulty",
        "game",
        "Before changing win rates",
        "balance",
        "Before changing win rates or placement",
        "-",
        "delegate",
        "y",
    )

    settings_ui._topics_page(dsn)

    with db.transaction(dsn) as cur:
        assert topics.get_topic(cur, "balance") is None
        assert topics.get_topic(cur, "difficulty") is None
        cur.execute(
            "SELECT event_type, detail FROM event_log WHERE event_type LIKE 'topic_%%' "
            "ORDER BY created_at"
        )
        events = cur.fetchall()
    assert [row["event_type"] for row in events][:2] == ["topic_created", "topic_updated"]
    edited = events[1]["detail"]["to"]
    assert edited["trigger"] == "Before changing win rates or placement"
    assert (edited["scope_id"], edited["action"]) == (None, "delegate")


def test_a_topic_holding_rules_is_not_removed_from_the_page(dsn, monkeypatch, capsys):
    with db.transaction(dsn) as cur:
        topic = topics.create_topic(cur, name="difficulty", trigger="Before tuning", actor="user")
        remember(cur, "keep the win rate even", delivery="topic", topic_id=topic["topic_id"])
    getkeys(monkeypatch, "x", "q")
    answers(monkeypatch, "y")

    settings_ui._topics_page(dsn)

    out = capsys.readouterr().out
    assert "still holds 1 active rule" in out
    assert "1 rule active" in out
    with db.transaction(dsn) as cur:
        assert topics.get_topic(cur, "difficulty")["archived_at"] is None
