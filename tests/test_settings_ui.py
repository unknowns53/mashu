from __future__ import annotations

import pytest

from conftest import candidate, expire, getkeys, new_project, new_task, remember
from mashu import (
    db,
    nominations,
    projects,
    routing,
    settings_ui,
    tasks,
    temporary,
    topics,
)
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


def test_health_collects_capacity_queues_evidence_and_task_activity(dsn):
    with db.transaction(dsn) as cur:
        project = projects.create_project(cur, name="health", summary="health checks", actor="user")
        remember(cur, "global health memory")
        remember(cur, "project health memory", project_id=project["project_id"], delivery="project")
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
            "INSERT INTO event_log (event_type, actor) "
            "VALUES ('delivery_failure_suspected', 'agent')"
        )

    value = settings_ui._health(dsn)

    assert value.schema_pending == ()
    assert value.memory_counts == {"always": 1, "project": 1}
    assert value.memory_always_tokens > 0 and value.memory_worst_tokens > 0
    assert value.active_states == 1 and value.state_worst_tokens > 0
    assert value.temporary_count == 1 and value.temporary_tokens > 0
    assert (value.pending_ready, value.pending_deferred) == (1, 1)
    assert value.ledger_30d >= 4
    assert value.delivery_failures_30d == 1 and value.project_count == 2
    assert (value.tasks_active, value.tasks_dormant, value.tasks_closed) == (1, 1, 1)


def test_bootstrap_preview_resolves_cwd_and_shows_every_payload_share(dsn, monkeypatch):
    here = "/tmp/mashu-settings-project"
    with db.transaction(dsn) as cur:
        project_id = projects.create_project(cur, name="preview", actor="user")["project_id"]
        routing.add_route(cur, path_prefix=here, project_id=project_id, actor="user")
        remember(cur, "always in preview")
        remember(cur, "project into preview", project_id=project_id, delivery="project")
        new_task(cur, "preview active state", "preview", goal="show the current state")
        temporary.put_temporary(
            cur, content="preview temporary context", project_id=project_id, actor="user", days=1
        )
    monkeypatch.setattr(settings_ui.os, "getcwd", lambda: f"{here}/nested")

    answer = settings_ui._bootstrap_preview(dsn)
    rendered = settings_ui._bootstrap_text(answer)

    assert answer["project"] == "preview" and answer["routed"] is True
    assert answer["always"][0]["content"] == "always in preview"
    assert answer["project_rules"][0]["content"] == "project into preview"
    assert answer["states"][0]["content"].startswith("preview active state")
    assert "call task_get" in answer["task_instruction"]
    assert answer["temporary"][0]["content"] == "preview temporary context"
    assert "Active task cards" in rendered
    assert "call task_get" in rendered
    assert "Tokens" in rendered and "Total" in rendered


@pytest.mark.parametrize("summary", ["Release and rollback knowledge", ""])
def test_project_page_summary_is_optional_and_a_duplicate_refused(
    dsn, monkeypatch, capsys, summary
):
    getkeys(monkeypatch, "n", "n", "left")
    answers(monkeypatch, "deployment", summary, "deployment", "another summary")

    settings_ui._projects_page(dsn)

    assert "already exists" in capsys.readouterr().out
    with db.transaction(dsn) as cur:
        row = projects.require_project(cur, "deployment")
    assert row["summary"] == (summary or None)


def test_ctrl_c_at_an_optional_project_summary_cancels_creation(dsn, monkeypatch):
    getkeys(monkeypatch, "n", "left")

    def answer(prompt: str) -> str:
        if prompt == "  project name: ":
            return "deployment"
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", answer)
    settings_ui._projects_page(dsn)

    with db.transaction(dsn) as cur:
        assert projects.get_project(cur, "deployment") is None


def test_project_name_and_summary_can_be_edited(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        project = projects.create_project(
            cur, name="old project", summary="old summary", actor="user"
        )
    getkeys(monkeypatch, "e", "left")
    answers(monkeypatch, "new project", "new summary")

    settings_ui._projects_page(dsn)

    with db.transaction(dsn) as cur:
        row = projects.require_project(cur, "new project")
    assert row["project_id"] == project["project_id"]
    assert row["summary"] == "new summary"


def test_project_refuses_banned_text(cur):
    for field in ("name", "summary"):
        with pytest.raises(RefusedError):
            projects.create_project(
                cur, actor="user", **{"name": "clean", "summary": "clean", field: "SECRETMARKER9"}
            )


def test_routes_can_be_edited_added_ignored_and_removed(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        project = projects.create_project(cur, name="route project", actor="user")
        route = routing.add_route(
            cur, path_prefix="/tmp/old", project_id=project["project_id"], actor="user"
        )
    getkeys(monkeypatch, "e", "n", "i", "x", "left")
    answers(
        monkeypatch, "/tmp/new", "-", "/tmp/project/a/long/path", "route project", "/tmp/i", "y"
    )

    settings_ui._routes_page(dsn)

    rows = settings_ui._route_rows(dsn)
    kept = [(row["path_prefix"], row["project_id"]) for row in rows]
    assert kept == [("/tmp/new", None), ("/tmp/i", None)]
    assert rows[0]["route_id"] == route["route_id"]


def test_schema_apply_is_confirmed_and_reports_a_noop(dsn, monkeypatch, capsys):
    called: list[str | None] = []
    getkeys(monkeypatch, "m", "left")
    prompts = answers(monkeypatch, "y")
    monkeypatch.setattr(
        settings_ui.migration, "migrate", lambda conninfo: called.append(conninfo) or []
    )

    settings_ui._schema_page(dsn)

    assert called == [dsn]
    assert prompts and "apply 0 pending migration" in prompts[0]
    assert "already up to date" in capsys.readouterr().out


def test_topic_page_creates_edits_and_removes_a_topic(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        projects.create_project(cur, name="game", actor="user")
    getkeys(monkeypatch, "n", "e", "x", "left")
    answers(
        monkeypatch,
        "difficulty",
        "game",
        "Before changing win rates",
        "balance",
        "Before changing win rates or placement",
        "-",
        "delegate",
        "Projects/Game/Balance.md#Win rates",
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
    assert (edited["project_id"], edited["action"]) == (None, "delegate")
    assert edited["source"] == "Projects/Game/Balance.md#Win rates"
