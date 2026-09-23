from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from conftest import expire, new_project, new_task, update_task
from mashu import task_history, tasks
from mashu.errors import (
    ClosedTaskError,
    MashuError,
    OverLimitError,
    ProjectBudgetError,
    RefusedError,
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


def checkpoint(cur, task, what_changed="recorded the state", **kwargs):
    return task_history.checkpoint(
        cur,
        task["task"]["task_id"],
        actor="agent",
        what_changed=what_changed,
        expect_updated_at=task["state"]["updated_at"],
        **kwargs,
    )


def _state_columns(row):
    return {field: row[field] for field in STATE_FIELDS}


def _history_counts(cur):
    counts = {}
    for table in ("task_checkpoint", "attempt", "decision", "artifact_reference"):
        cur.execute(f"SELECT count(*) AS n FROM {table}")
        counts[table] = cur.fetchone()["n"]
    return counts


def test_checkpoint_replaces_and_freezes_the_same_state(cur, task):
    checkpointed = checkpoint(
        cur,
        task,
        "history service is in place",
        status_text="the append-only history rows are being wired",
        next_actions=["run the PostgreSQL tests"],
    )

    cur.execute("SELECT * FROM task_state WHERE task_id = %s", (task["task"]["task_id"],))
    frozen = checkpointed["checkpoint"]
    assert _state_columns(cur.fetchone()) == _state_columns(frozen)
    assert frozen["what_changed"] == "history service is in place"
    assert frozen["created_by"] == "agent"
    assert frozen["evidence"] == []

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'task_checkpointed'")
    detail = cur.fetchone()["detail"]
    assert detail["task_id"] == str(task["task"]["task_id"])
    assert detail["checkpoint_id"] == str(frozen["checkpoint_id"])
    assert detail["evidence"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("attempt", "x" * 301), ("result", "x" * 501), ("reason", "x" * 501), ("next", "x" * 301)],
)
def test_attempt_limit_is_refused_before_an_attempt_row(cur, task_id, field, value):
    with pytest.raises(OverLimitError) as raised:
        task_history.attempt_record(
            cur, task_id, actor="agent", **{"attempt": "try it", field: value}
        )
    assert raised.value.field == field
    assert _history_counts(cur)["attempt"] == 0


def test_a_refused_checkpoint_leaves_no_frozen_row(cur, task, monkeypatch):
    with pytest.raises(OverLimitError):
        checkpoint(cur, task, goal="x" * 301)

    other = new_task(cur, "other task", "history")
    foreign = task_history.artifact_link(
        cur, other["task"]["task_id"], actor="agent", kind="file", locator="results/other.txt"
    )
    for evidence in ([uuid4()], [foreign["reference_id"]]):
        with pytest.raises(MashuError):
            checkpoint(cur, task, evidence=evidence)

    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", "1")
    with pytest.raises(ProjectBudgetError):
        checkpoint(
            cur,
            task,
            goal=task["state"]["goal"],
            next_actions=task["state"]["next_actions"],
            status_text="this state cannot fit, and it is larger than the one it replaces",
        )
    monkeypatch.delenv("MASHU_PROJECT_CAPACITY")

    update_task(cur, task["task"]["task_id"], actor="other", status_text="the other session won")
    with pytest.raises(MashuError):
        checkpoint(cur, task, status_text="still stale")

    assert _history_counts(cur)["task_checkpoint"] == 0


def test_linked_artifacts_of_the_same_task_join_the_checkpoint_evidence(cur, task, task_id):
    earlier = task_history.artifact_link(
        cur, task_id, actor="agent", kind="file", locator="results/table.csv", label="the table"
    )
    checkpointed = checkpoint(
        cur,
        task,
        evidence=[earlier["reference_id"]],
        artifacts=[{"kind": "git_commit", "locator": "0bd5d89"}],
    )
    linked = checkpointed["artifacts"][0]["reference_id"]
    assert checkpointed["checkpoint"]["evidence"] == [earlier["reference_id"], linked]


def test_history_writes_renew_the_lease(cur, task_id):
    lease = "SELECT active_until, last_activity_at FROM task WHERE task_id = %s"
    for write in (
        lambda: task_history.attempt_record(cur, task_id, actor="agent", attempt="try a"),
        lambda: task_history.decision_record(cur, task_id, actor="agent", decision="keep it"),
        lambda: task_history.artifact_link(
            cur, task_id, actor="agent", kind="url", locator="https://example.test"
        ),
    ):
        expire(cur, task_id)
        before = cur.execute(lease, (task_id,)).fetchone()
        assert write()["task_id"] == task_id
        after = cur.execute(lease, (task_id,)).fetchone()
        assert after["active_until"] > before["active_until"]
        assert after["last_activity_at"] > before["last_activity_at"]


def test_closed_task_refuses_all_four_history_writes(cur, task_id):
    closed = tasks.close(cur, task_id, outcome="completed", actor="user")

    with pytest.raises(ClosedTaskError):
        checkpoint(cur, closed, "too late")
    with pytest.raises(ClosedTaskError):
        task_history.attempt_record(cur, task_id, actor="agent", attempt="too late")
    with pytest.raises(ClosedTaskError):
        task_history.decision_record(cur, task_id, actor="agent", decision="too late")
    with pytest.raises(ClosedTaskError):
        task_history.artifact_link(cur, task_id, actor="agent", kind="file", locator="too late")


def test_decisions_form_a_same_task_supersedes_chain(cur, task_id):
    first = task_history.decision_record(cur, task_id, actor="agent", decision="use one module")
    second = task_history.decision_record(
        cur,
        task_id,
        actor="agent",
        decision="keep the module boundary",
        supersedes_id=first["decision_id"],
    )
    other = new_task(cur, "different task", "history")

    with pytest.raises(MashuError):
        task_history.decision_record(
            cur,
            other["task"]["task_id"],
            actor="agent",
            decision="cannot cross the boundary",
            supersedes_id=first["decision_id"],
        )

    decisions = task_history.decision_list(cur, task_id)
    assert [row["decision_id"] for row in decisions] == [
        second["decision_id"],
        first["decision_id"],
    ]
    assert decisions[0]["supersedes_id"] == first["decision_id"]


def test_close_freezes_the_last_state_with_the_closing_actor(cur, task_id):
    updated = update_task(
        cur, task_id, status_text="ready for the user's judgement", next_actions=["close it"]
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


def test_expanded_task_contains_only_the_requested_histories(cur, task, task_id):
    task_history.attempt_record(cur, task_id, actor="agent", attempt="try it")
    task_history.decision_record(cur, task_id, actor="agent", decision="keep it")
    task_history.artifact_link(cur, task_id, actor="agent", kind="file", locator="src/main.py")
    checkpoint(cur, task)

    expanded = task_history.expanded_task(cur, task_id, attempts=True, artifacts=True)
    assert len(expanded["attempts"]) == 1
    assert len(expanded["artifacts"]) == 1
    assert "decisions" not in expanded
    assert "checkpoints" not in expanded
    assert "task" in expanded and "state" in expanded


def test_artifact_locator_is_still_checked_as_text(cur, task_id, monkeypatch, tmp_path):
    patterns = tmp_path / "banned-patterns"
    patterns.write_text(re.escape(str(Path.home())) + "\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(patterns))

    with pytest.raises(RefusedError):
        task_history.artifact_link(
            cur,
            task_id,
            actor="agent",
            kind="file",
            locator=str(Path.home() / "private" / "result.txt"),
        )


def test_checkpoint_writes_attempts_decisions_and_artifacts_with_the_state(cur, task, task_id):
    earlier = task_history.decision_record(cur, task_id, actor="agent", decision="use one module")
    checkpointed = checkpoint(
        cur,
        task,
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
    assert len(task_history.attempt_list(cur, task_id)) == 2
    assert len(task_history.decision_list(cur, task_id)) == 2
    assert len(task_history.artifact_list(cur, task_id)) == 1
    assert checkpointed["checkpoint"]["created_by"] == "agent"
    assert checkpointed["unchecked"] is False


def test_one_refused_item_leaves_the_checkpoint_unwritten(cur, task, task_id):
    before = cur.execute("SELECT * FROM task_state WHERE task_id = %s", (task_id,)).fetchone()

    with pytest.raises(OverLimitError) as raised:
        checkpoint(
            cur,
            task,
            status_text="this must not land",
            attempts=[{"attempt": "fine"}, {"attempt": "x" * 301}],
            decisions=[{"decision": "would be recorded"}],
            artifacts=[{"kind": "file", "locator": "src/main.py"}],
        )

    assert raised.value.field == "attempt"
    after = cur.execute("SELECT * FROM task_state WHERE task_id = %s", (task_id,)).fetchone()
    assert after == before
    assert set(_history_counts(cur).values()) == {0}


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ({"attempts": [{"attempt": "try", "outcome": "worked"}]}, "unknown key"),
        ({"decisions": [{"reason": "no decision given"}]}, "missing required"),
        ({"artifacts": [{"kind": "file"}]}, "missing required"),
        ({"artifacts": [{"kind": "tarball", "locator": "a.tgz"}]}, "unknown artifact kind"),
        ({"decisions": [{"decision": "d", "supersedes_id": "not-a-uuid"}]}, "UUID"),
        ({"attempts": ["just a string"]}, "must be an object"),
    ],
)
def test_malformed_history_items_are_refused_before_any_write(cur, task, items, message):
    with pytest.raises(MashuError, match=message):
        checkpoint(cur, task, "malformed history", **items)
    assert set(_history_counts(cur).values()) == {0}


def test_history_is_written_only_through_task_checkpoint_over_mcp(monkeypatch):
    pytest.importorskip("mcp.server")
    import asyncio

    from mashu import server

    monkeypatch.setenv("MASHU_DATABASE_URL", "dbname=mashu_test_never_created")
    names = {tool.name for tool in asyncio.run(server.build_server().list_tools())}
    assert len(names) == 19
    assert not names & {"attempt_record", "decision_record", "artifact_link"}
    assert "task_checkpoint" in names
