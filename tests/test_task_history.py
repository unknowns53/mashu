"""Task history, which records the work without becoming a second current state."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from mashu import projects, task_history, tasks
from mashu.errors import (
    ClosedTaskError,
    MashuError,
    OverLimitError,
    ProjectBudgetError,
    RefusedError,
)


@pytest.fixture
def project(cur):
    return projects.create_project(cur, name="history", actor="user")


@pytest.fixture
def task(cur, project):
    return tasks.task_create(
        cur,
        project=project["project_id"],
        name="record the task history",
        actor="agent",
        goal="keep the current state and its milestones consistent",
        next_actions=["write the history module"],
    )


def _state_columns(row):
    return {
        field: row[field]
        for field in (
            "goal",
            "approach",
            "status_text",
            "open_questions",
            "blockers",
            "next_actions",
        )
    }


def _expire(cur, task_id):
    cur.execute(
        "UPDATE task SET active_until = now() - interval '1 day' WHERE task_id = %s",
        (task_id,),
    )


def test_checkpoint_replaces_and_freezes_the_same_state(cur, task):
    checkpointed = task_history.checkpoint(
        cur,
        task["task"]["task_id"],
        actor="agent",
        what_changed="history service is in place",
        expect_updated_at=task["state"]["updated_at"],
        status_text="the append-only history rows are being wired",
        next_actions=["run the PostgreSQL tests"],
    )

    cur.execute(
        "SELECT * FROM task_state WHERE task_id = %s",
        (task["task"]["task_id"],),
    )
    state = cur.fetchone()
    frozen = checkpointed["checkpoint"]
    assert _state_columns(state) == _state_columns(frozen)
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
def test_attempt_limit_is_refused_before_an_attempt_row(cur, task, field, value):
    with pytest.raises(OverLimitError) as raised:
        task_history.attempt_record(
            cur,
            task["task"]["task_id"],
            actor="agent",
            **{"attempt": "try it", field: value},
        )
    assert raised.value.field == field
    cur.execute("SELECT count(*) AS n FROM attempt")
    assert cur.fetchone()["n"] == 0


def test_checkpoint_limit_leaves_no_frozen_row(cur, task):
    with pytest.raises(OverLimitError):
        task_history.checkpoint(
            cur,
            task["task"]["task_id"],
            actor="agent",
            what_changed="state was changed",
            expect_updated_at=task["state"]["updated_at"],
            goal="x" * 301,
        )

    cur.execute("SELECT count(*) AS n FROM task_checkpoint")
    assert cur.fetchone()["n"] == 0


def test_a_stale_checkpoint_leaves_no_frozen_row(cur, task):
    tasks.task_update(
        cur,
        task["task"]["task_id"],
        actor="other session",
        expect_updated_at=task["state"]["updated_at"],
        status_text="the other session won",
    )
    with pytest.raises(MashuError):
        task_history.checkpoint(
            cur,
            task["task"]["task_id"],
            actor="agent",
            what_changed="this version lost",
            expect_updated_at=task["state"]["updated_at"],
            status_text="still stale",
        )

    cur.execute("SELECT count(*) AS n FROM task_checkpoint")
    assert cur.fetchone()["n"] == 0


def test_a_budget_refusal_leaves_no_frozen_row(cur, task, monkeypatch):
    tasks.task_create(cur, project="history", name="another active task", actor="agent")
    monkeypatch.setenv("MASHU_PROJECT_CAPACITY", "1")

    with pytest.raises(ProjectBudgetError):
        task_history.checkpoint(
            cur,
            task["task"]["task_id"],
            actor="agent",
            what_changed="would exceed the project state share",
            expect_updated_at=task["state"]["updated_at"],
            status_text="this state cannot fit",
        )

    cur.execute("SELECT count(*) AS n FROM task_checkpoint")
    assert cur.fetchone()["n"] == 0


def test_checkpoint_evidence_must_exist_and_belong_to_the_task(cur, task, project):
    other = tasks.task_create(cur, project=project["project_id"], name="other task", actor="agent")
    foreign = task_history.artifact_link(
        cur,
        other["task"]["task_id"],
        actor="agent",
        kind="file",
        locator="results/other.txt",
    )
    task_id = task["task"]["task_id"]

    for evidence in ([uuid4()], [foreign["reference_id"]]):
        with pytest.raises(MashuError):
            task_history.checkpoint(
                cur,
                task_id,
                actor="agent",
                what_changed="evidence should be refused",
                expect_updated_at=task["state"]["updated_at"],
                evidence=evidence,
            )

    cur.execute("SELECT count(*) AS n FROM task_checkpoint")
    assert cur.fetchone()["n"] == 0


def test_checkpoint_accepts_artifacts_from_the_same_task(cur, task):
    artifact = task_history.artifact_link(
        cur,
        task["task"]["task_id"],
        actor="agent",
        kind="git_commit",
        locator="9d12f5a",
        label="history implementation",
    )
    checkpointed = task_history.checkpoint(
        cur,
        task["task"]["task_id"],
        actor="agent",
        what_changed="linked the implementation",
        expect_updated_at=task["state"]["updated_at"],
        evidence=[artifact["reference_id"]],
    )
    assert checkpointed["checkpoint"]["evidence"] == [artifact["reference_id"]]


def test_history_writes_renew_the_lease(cur, task):
    task_id = task["task"]["task_id"]
    _expire(cur, task_id)
    before = cur.execute(
        "SELECT active_until, last_activity_at FROM task WHERE task_id = %s", (task_id,)
    ).fetchone()
    task_history.attempt_record(cur, task_id, actor="agent", attempt="try a")
    after_attempt = cur.execute(
        "SELECT active_until, last_activity_at FROM task WHERE task_id = %s", (task_id,)
    ).fetchone()
    assert after_attempt["active_until"] > before["active_until"]
    assert after_attempt["last_activity_at"] > before["last_activity_at"]

    _expire(cur, task_id)
    before_decision = cur.execute(
        "SELECT active_until FROM task WHERE task_id = %s", (task_id,)
    ).fetchone()
    decision = task_history.decision_record(cur, task_id, actor="agent", decision="keep it")
    after_decision = cur.execute(
        "SELECT active_until FROM task WHERE task_id = %s", (task_id,)
    ).fetchone()
    assert after_decision["active_until"] > before_decision["active_until"]

    _expire(cur, task_id)
    before_artifact = cur.execute(
        "SELECT active_until FROM task WHERE task_id = %s", (task_id,)
    ).fetchone()
    task_history.artifact_link(
        cur, task_id, actor="agent", kind="url", locator="https://example.test"
    )
    after_artifact = cur.execute(
        "SELECT active_until FROM task WHERE task_id = %s", (task_id,)
    ).fetchone()
    assert after_artifact["active_until"] > before_artifact["active_until"]
    assert decision["task_id"] == task_id


def test_closed_task_refuses_all_four_history_writes(cur, task):
    task_id = task["task"]["task_id"]
    closed = tasks.close(cur, task_id, outcome="completed", actor="user")

    with pytest.raises(ClosedTaskError):
        task_history.checkpoint(
            cur,
            task_id,
            actor="agent",
            what_changed="too late",
            expect_updated_at=closed["state"]["updated_at"],
        )
    with pytest.raises(ClosedTaskError):
        task_history.attempt_record(cur, task_id, actor="agent", attempt="too late")
    with pytest.raises(ClosedTaskError):
        task_history.decision_record(cur, task_id, actor="agent", decision="too late")
    with pytest.raises(ClosedTaskError):
        task_history.artifact_link(cur, task_id, actor="agent", kind="file", locator="too late")


def test_decisions_form_a_same_task_supersedes_chain(cur, task, project):
    first = task_history.decision_record(
        cur, task["task"]["task_id"], actor="agent", decision="use one module"
    )
    second = task_history.decision_record(
        cur,
        task["task"]["task_id"],
        actor="agent",
        decision="keep the module boundary",
        supersedes_id=first["decision_id"],
    )
    other = tasks.task_create(
        cur, project=project["project_id"], name="different task", actor="agent"
    )

    with pytest.raises(MashuError):
        task_history.decision_record(
            cur,
            other["task"]["task_id"],
            actor="agent",
            decision="cannot cross the boundary",
            supersedes_id=first["decision_id"],
        )

    decisions = task_history.decision_list(cur, task["task"]["task_id"])
    assert [row["decision_id"] for row in decisions] == [
        second["decision_id"],
        first["decision_id"],
    ]
    assert decisions[0]["supersedes_id"] == first["decision_id"]


def test_close_freezes_the_last_state_with_the_closing_actor(cur, task):
    task_id = task["task"]["task_id"]
    updated = tasks.task_update(
        cur,
        task_id,
        actor="agent",
        expect_updated_at=task["state"]["updated_at"],
        status_text="ready for the user's judgement",
        next_actions=["close the task"],
    )
    closed = tasks.close(
        cur,
        task_id,
        outcome="completed",
        reason="the implementation is merged",
        actor="user",
    )
    checkpoints = task_history.checkpoint_list(cur, task_id)
    assert len(checkpoints) == 1
    frozen = checkpoints[0]
    assert _state_columns(frozen) == _state_columns(updated["state"])
    assert frozen["what_changed"] == "closed as completed: the implementation is merged"
    assert frozen["created_by"] == "user"
    assert closed["task"]["status"] == "closed"


def test_expanded_task_contains_only_the_requested_histories(cur, task):
    task_id = task["task"]["task_id"]
    task_history.attempt_record(cur, task_id, actor="agent", attempt="try it")
    task_history.decision_record(cur, task_id, actor="agent", decision="keep it")
    task_history.artifact_link(cur, task_id, actor="agent", kind="file", locator="src/main.py")
    task_history.checkpoint(
        cur,
        task_id,
        actor="agent",
        what_changed="recorded the current state",
        expect_updated_at=task["state"]["updated_at"],
    )

    expanded = task_history.expanded_task(cur, task_id, attempts=True, artifacts=True)
    assert len(expanded["attempts"]) == 1
    assert len(expanded["artifacts"]) == 1
    assert "decisions" not in expanded
    assert "checkpoints" not in expanded
    assert "task" in expanded and "state" in expanded


def test_checkpoint_rows_refuse_update_at_the_database_boundary(cur, task):
    task_id = task["task"]["task_id"]
    cur.execute(
        "INSERT INTO task_checkpoint (task_id, what_changed, created_by) VALUES (%s, %s, %s)",
        (task_id, "the state was frozen", "agent"),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("UPDATE task_checkpoint SET what_changed = 'rewritten'")


def test_checkpoint_rows_refuse_delete_at_the_database_boundary(cur, task):
    task_id = task["task"]["task_id"]
    cur.execute(
        "INSERT INTO task_checkpoint (task_id, what_changed, created_by) VALUES (%s, %s, %s)",
        (task_id, "the state was frozen", "agent"),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("DELETE FROM task_checkpoint")


def test_artifact_locator_is_still_checked_as_text(cur, task, monkeypatch, tmp_path):
    patterns = tmp_path / "banned-patterns"
    patterns.write_text(re.escape(str(Path.home())) + "\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(patterns))

    with pytest.raises(RefusedError):
        task_history.artifact_link(
            cur,
            task["task"]["task_id"],
            actor="agent",
            kind="file",
            locator=str(Path.home() / "private" / "result.txt"),
        )
