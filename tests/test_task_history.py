from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

import pytest

from conftest import expire, new_project, new_task, update_task
from mashu import approvals, db, redact, server, task_history, tasks
from mashu.errors import (
    ClosedTaskError,
    DuplicateTaskError,
    MalformedRequestError,
    MashuError,
    ProjectBudgetError,
    RefusedError,
    StaleStateError,
)

STATE_FIELDS = ("goal", "approach", "status_text", "open_questions", "blockers", "next_actions")


@pytest.fixture
def task(cur):
    new_project(cur, "history")
    return new_task(
        cur,
        "record the task history",
        "history",
        goal="keep the current state and its milestones consistent",
        next_actions=["write the history module"],
    )


@pytest.fixture
def task_id(task):
    return task["task"]["task_id"]


def checkpoint(cur, task, what_changed=None, **kwargs):
    return task_history.checkpoint(
        cur,
        task["task"]["task_id"],
        actor="agent",
        what_changed=what_changed,
        expect_updated_at=task["state"]["updated_at"],
        **kwargs,
    )


def _replaced_details(cur, task_id):
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'task_state_replaced' "
        "AND detail->>'task_id' = %s ORDER BY created_at, event_id",
        (str(task_id),),
    )
    return [row["detail"] for row in cur.fetchall()]


def _state_columns(row):
    return {field: row[field] for field in STATE_FIELDS}


def _history_counts(cur):
    counts = {}
    for table in ("task_checkpoint", "attempt", "decision", "artifact_reference"):
        cur.execute(f"SELECT count(*) AS n FROM {table}")
        counts[table] = cur.fetchone()["n"]
    return counts


def test_repeated_checkpoints_replace_the_state_without_freezing_it(cur, task, task_id):
    first = checkpoint(
        cur,
        task,
        "history service is in place",
        status_text="the append-only history rows are being wired",
        next_actions=["run the PostgreSQL tests"],
    )
    second = checkpoint(cur, first, approach="one module", next_actions=[])

    cur.execute("SELECT * FROM task_state WHERE task_id = %s", (task_id,))
    stored = cur.fetchone()
    assert stored["status_text"] == "the append-only history rows are being wired"
    assert (stored["approach"], stored["next_actions"]) == ("one module", [])
    assert _state_columns(stored) == _state_columns(second["state"])
    assert "checkpoint" not in second and "cadence" not in second
    assert task_history.checkpoint_list(cur, task_id) == []
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'task_checkpointed'")
    assert cur.fetchone()["n"] == 0

    written, unexplained = _replaced_details(cur, task_id)
    assert written["what_changed"] == "history service is in place"
    assert "what_changed" not in unexplained


def test_a_checkpoint_renames_the_task_unless_the_name_reads_like_another(cur, task, task_id):
    renamed = checkpoint(cur, task, name="keep the task history", status_text="renamed")
    assert renamed["task"]["name"] == "keep the task history"
    assert renamed["state"]["status_text"] == "renamed"
    detail = _replaced_details(cur, task_id)[-1]
    assert (detail["from_name"], detail["to_name"]) == (
        "record the task history",
        "keep the task history",
    )

    other = new_task(cur, "publish the release notes", "history")
    with pytest.raises(DuplicateTaskError):
        checkpoint(cur, other, name="keep the task history")
    assert tasks.task_get(cur, other["task"]["task_id"])["task"]["name"] == (
        "publish the release notes"
    )


ITEMS = {
    "attempts": [{"attempt": "try the narrow fix", "result": "too narrow"}],
    "decisions": [{"decision": "keep one module"}],
    "artifacts": [{"kind": "file", "locator": "results/table.csv"}],
}


def test_a_refused_state_write_leaves_its_history_unwritten(cur, task, monkeypatch):
    with pytest.raises(MalformedRequestError):
        checkpoint(cur, task, goal="x" * 301, **ITEMS)

    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", "1")
    with pytest.raises(ProjectBudgetError):
        checkpoint(
            cur,
            task,
            goal=task["state"]["goal"],
            next_actions=task["state"]["next_actions"],
            status_text="this state cannot fit, and it is larger than the one it replaces",
            **ITEMS,
        )
    monkeypatch.delenv("MASHU_PROJECT_CAPACITY")

    update_task(cur, task["task"]["task_id"], actor="other", status_text="the other session won")
    with pytest.raises(StaleStateError):
        checkpoint(cur, task, status_text="still stale", **ITEMS)

    assert set(_history_counts(cur).values()) == {0}


def test_a_checkpoint_renews_the_lease_and_is_refused_once_the_task_is_closed(cur, task, task_id):
    lease = "SELECT active_until, last_activity_at FROM task WHERE task_id = %s"
    expire(cur, task_id)
    before = cur.execute(lease, (task_id,)).fetchone()
    checkpoint(cur, task, attempts=[{"attempt": "try a"}])
    after = cur.execute(lease, (task_id,)).fetchone()
    assert after["active_until"] > before["active_until"]
    assert after["last_activity_at"] > before["last_activity_at"]

    closed = tasks.close(cur, task_id, outcome="completed", actor="user")
    with pytest.raises(ClosedTaskError):
        checkpoint(cur, closed, "too late")


def test_decisions_form_a_same_task_supersedes_chain(cur, task, task_id):
    first = checkpoint(cur, task, decisions=[{"decision": "use one module"}])["decisions"][0]
    later = {"decision": "keep the module boundary", "supersedes_id": first["decision_id"]}
    second = checkpoint(cur, tasks.task_get(cur, task_id), decisions=[later])["decisions"][0]
    other = new_task(cur, "different task", "history")

    with pytest.raises(MashuError):
        checkpoint(cur, other, decisions=[{**later, "decision": "cannot cross the boundary"}])

    decisions = task_history.decision_list(cur, task_id)
    assert [row["decision_id"] for row in decisions] == [
        second["decision_id"],
        first["decision_id"],
    ]
    assert decisions[0]["supersedes_id"] == first["decision_id"]


def test_close_freezes_the_last_state_with_the_closing_actor(cur, task, task_id):
    handed = checkpoint(cur, task, "wired it", status_text="wired")
    updated = checkpoint(
        cur, handed, status_text="ready for the user's judgement", next_actions=["close it"]
    )
    closed = tasks.close(
        cur, task_id, outcome="completed", reason="the implementation is merged", actor="user"
    )
    checkpoints = task_history.checkpoint_list(cur, task_id)
    assert len(checkpoints) == 1
    frozen = checkpoints[0]
    assert _state_columns(frozen) == _state_columns(updated["state"])
    assert frozen["what_changed"] == "closed as completed: the implementation is merged"
    assert frozen["created_by"] == "user"
    assert closed["task"]["status"] == "closed"


def test_artifact_locator_keeps_the_home_directory_from_tilde(cur, task, monkeypatch, tmp_path):
    home = str(Path.home())
    (tmp_path / "patterns").write_text(re.escape(home) + "\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "patterns"))

    written = checkpoint(
        cur, task, artifacts=[{"kind": "file", "locator": f"in {home}/result.txt"}]
    )

    assert written["artifacts"][0]["locator"] == "in ~/result.txt"


def test_artifact_locator_is_checked_and_named_without_echoing_it(cur, task, monkeypatch, tmp_path):
    (tmp_path / "patterns").write_text("forbidden\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "patterns"))

    with pytest.raises(RefusedError) as refused:
        checkpoint(cur, task, artifacts=[{"kind": "file", "locator": "in forbidden/result.txt"}])
    answer = server._failure(refused.value)
    assert (answer["field"], answer["span"]) == ("artifacts[0].locator", [3, 12])
    assert str(refused.value).startswith("artifacts[0].locator matches banned pattern #0 at ")
    assert "forbidden" not in str(refused.value)


def test_refusal_says_how_to_rewrite_by_where_the_match_sits(monkeypatch, tmp_path):
    user = Path.home().name
    (tmp_path / "patterns").write_text(re.escape(user) + "\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "patterns"))
    scratch = f"/private/tmp/claude-501/{str(Path.home()).replace('/', '-')}-MyApp/notes.md"

    named = redact.check({"next_actions[0]": f"show it to {user} first"})
    in_scratch = redact.check({"artifacts[0].locator": scratch})

    assert not named.in_home and "refer to people by role" in named.reason()
    assert in_scratch.in_home and "write the path from ~" in in_scratch.reason()
    assert user not in named.reason() + in_scratch.reason()


def test_a_windows_home_is_written_from_tilde_in_each_spelling_an_agent_uses(monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: PureWindowsPath(r"C:\Users\SECRETMARKER1"))
    spellings = [
        r"C:\Users\SECRETMARKER1\.claude\projects\C--Users-SECRETMARKER1-adapter\a.jsonl",
        "C:/Users/SECRETMARKER1/.claude/projects/C--Users-SECRETMARKER1-adapter/a.jsonl",
    ]

    assert [approvals.home_relative(text) for text in spellings] == [
        r"~\.claude\projects\~-adapter\a.jsonl",
        "~/.claude/projects/~-adapter/a.jsonl",
    ]


def test_checkpoint_writes_attempts_decisions_and_artifacts_with_the_state(cur, task, task_id):
    first = checkpoint(
        cur,
        task,
        decisions=[{"decision": "use one module"}],
        artifacts=[{"kind": "file", "locator": "results/table.csv", "label": "the table"}],
    )
    earlier, linked = first["decisions"][0], first["artifacts"][0]
    checkpointed = checkpoint(
        cur,
        first,
        "history arrives with the checkpoint",
        status_text="checkpoint carries its history",
        attempts=[
            {"attempt": "append rows in separate calls", "result": "too many calls"},
            {"attempt": "one call", "reason": "fewer verbs", "next": "measure it"},
        ],
        decisions=[
            {
                "decision": "keep the module boundary",
                "reason": "the CLI still reads the rows",
                "supersedes_id": str(earlier["decision_id"]),
            }
        ],
        artifacts=[{"kind": "git_commit", "locator": "9d12f5a", "label": "fold history"}],
    )

    assert [row["attempt"] for row in checkpointed["attempts"]] == [
        "append rows in separate calls",
        "one call",
    ]
    assert checkpointed["attempts"][1]["next"] == "measure it"
    assert checkpointed["decisions"][0]["supersedes_id"] == earlier["decision_id"]
    assert checkpointed["artifacts"][0]["label"] == "fold history"
    assert checkpointed["state"]["status_text"] == "checkpoint carries its history"
    assert len(task_history.decision_list(cur, task_id)) == 2
    assert checkpointed["unchecked"] is False
    assert task_history.checkpoint_list(cur, task_id) == []

    detail = _replaced_details(cur, task_id)[-1]
    assert detail["what_changed"] == "history arrives with the checkpoint"
    assert detail["attempt_ids"] == [str(row["attempt_id"]) for row in checkpointed["attempts"]]
    assert detail["decision_ids"] == [str(checkpointed["decisions"][0]["decision_id"])]
    assert detail["reference_ids"] == [str(checkpointed["artifacts"][0]["reference_id"])]
    assert str(linked["reference_id"]) not in detail["reference_ids"]

    expanded = task_history.expanded_task(cur, task_id, attempts=True, artifacts=True)
    assert len(expanded["attempts"]) == 2
    assert len(expanded["artifacts"]) == 2
    assert expanded["artifacts"][0]["created_at"] > expanded["artifacts"][1]["created_at"]
    assert "decisions" not in expanded
    assert "checkpoints" not in expanded
    assert "task" in expanded and "state" in expanded


@pytest.mark.parametrize(
    ("items", "message"),
    [
        (
            {
                "attempts": [{"attempt": "fine"}, {"attempt": "x" * 301}],
                "decisions": [{"decision": "would be recorded"}],
                "artifacts": [{"kind": "file", "locator": "src/main.py"}],
            },
            r"^attempts\[1\]\.attempt is 301 chars and the limit is 300",
        ),
        (
            {"attempts": [{"attempt": "try", "result": "x" * 501}]},
            r"^attempts\[0\]\.result is 501 chars",
        ),
        (
            {"attempts": [{"attempt": "try", "reason": "x" * 501}]},
            r"^attempts\[0\]\.reason is 501 chars",
        ),
        (
            {"attempts": [{"attempt": "try", "next": "x" * 301}]},
            r"^attempts\[0\]\.next is 301 chars",
        ),
        ({"attempts": [{"attempt": "try", "outcome": "worked"}]}, "unknown key"),
        ({"decisions": [{"reason": "no decision given"}]}, "missing required"),
        ({"artifacts": [{"kind": "file"}]}, "missing required"),
        ({"artifacts": [{"kind": "tarball", "locator": "a.tgz"}]}, r"artifacts\[0\]\.kind"),
        ({"decisions": [{"decision": "d", "supersedes_id": "not-a-uuid"}]}, "UUID"),
        ({"attempts": ["just a string"]}, "must be an object"),
    ],
)
def test_one_refused_item_leaves_the_checkpoint_unwritten(cur, task, task_id, items, message):
    state = "SELECT * FROM task_state WHERE task_id = %s"
    before = cur.execute(state, (task_id,)).fetchone()
    with pytest.raises(MashuError, match=message):
        checkpoint(cur, task, status_text="this must not land", **items)
    assert cur.execute(state, (task_id,)).fetchone() == before
    assert set(_history_counts(cur).values()) == {0}


def test_mcp_writes_history_only_through_task_checkpoint_and_publishes_write_contracts(monkeypatch):
    pytest.importorskip("mcp.server")
    import asyncio

    monkeypatch.setenv("MASHU_DATABASE_URL", "dbname=mashu_test_never_created")
    tools = {tool.name: tool for tool in asyncio.run(server.build_server().list_tools())}
    assert not set(tools) & {"attempt_record", "decision_record", "artifact_link", "task_update"}

    outcome = tools["task_propose_close"].input_schema["properties"]["outcome"]
    assert outcome["enum"] == list(tasks.OUTCOMES)
    history = tools["task_checkpoint"].input_schema["properties"]
    assert "evidence" not in history and "name" in history
    required = tools["task_checkpoint"].input_schema["required"]
    assert sorted(required) == ["expect_updated_at", "task_id"]
    assert "cadence" not in tools["task_checkpoint"].description
    items = {name: history[name]["anyOf"][0]["items"] for name in task_history.ITEM_KEYS}
    for name, (required, optional) in task_history.ITEM_KEYS.items():
        assert items[name]["required"] == list(required)
        assert set(items[name]["properties"]) == {*required, *optional}
    assert items["artifacts"]["properties"]["kind"]["enum"] == list(task_history.ARTIFACT_KINDS)
    for name in ("task_create", "task_checkpoint"):
        assert f"status_text {tasks.TEXT_LIMITS['status_text']}" in tools[name].description
    limits = task_history.HISTORY_LIMITS
    assert f"what_changed {limits['what_changed']}" in tools["task_checkpoint"].description


def test_one_refused_checkpoint_answers_with_every_problem_over_mcp(mcp, dsn):
    with db.transaction(dsn) as cur:
        new_project(cur, "history")
        made = new_task(cur, "record the task history", "history")
    too_many = [f"action {n}" for n in range(tasks.LIST_MAX_ITEMS + 1)]
    refused = mcp(
        "task_checkpoint",
        task_id=str(made["task"]["task_id"]),
        expect_updated_at=made["state"]["updated_at"].isoformat(),
        what_changed="x" * (task_history.HISTORY_LIMITS["what_changed"] + 1),
        next_actions=too_many,
        attempts=[{"result": "the attempt itself is missing"}],
    )
    assert refused["ok"] is False
    assert [(p["field"], p["limit"]) for p in refused["over_limit"]] == [
        ("what_changed", task_history.HISTORY_LIMITS["what_changed"]),
        ("next_actions", tasks.LIST_MAX_ITEMS),
    ]
    assert len(refused["problems"]) == 3
    assert refused["problems"][2].startswith("attempts[0]")


def test_frozen_checkpoints_written_before_still_come_back_from_task_get(mcp, dsn):
    with db.transaction(dsn) as cur:
        new_project(cur, "history")
        made = new_task(cur, "record the task history", "history", goal="keep milestones")
        task_id = made["task"]["task_id"]
        linked = checkpoint(cur, made, artifacts=[{"kind": "git_commit", "locator": "9d12f5a"}])
        reference_id = linked["artifacts"][0]["reference_id"]
        cur.execute(
            """
            INSERT INTO task_checkpoint (task_id, what_changed, goal, evidence, created_by)
            VALUES (%s, 'wired the history rows', 'keep milestones', %s, 'agent')
            """,
            (task_id, [reference_id]),
        )
    at = mcp("task_get", task_id=str(task_id))["state"]["updated_at"]
    written = mcp("task_checkpoint", task_id=str(task_id), expect_updated_at=at, status_text="on")
    assert written["ok"] is True

    got = mcp("task_get", task_id=str(task_id), checkpoints=True)
    assert [row["what_changed"] for row in got["checkpoints"]] == ["wired the history rows"]
    assert got["checkpoints"][0]["evidence"] == [str(reference_id)]
    assert got["checkpoints"][0]["created_at"]
