from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from uuid import uuid4

import pytest

from conftest import expire, new_project, new_task, update_task
from mashu import approvals, db, redact, server, settings, task_history, tasks
from mashu.errors import (
    ClosedTaskError,
    DuplicateTaskError,
    MalformedRequestError,
    MashuError,
    OverLimitError,
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
    for table in (
        "task_checkpoint",
        "attempt",
        "decision",
        "ruling",
        "ruling_withdrawal",
        "artifact_reference",
    ):
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


RULE = {"ruling": "the baseline is Standard", "user_instruction": "人間の基準を Standard にしろ"}

ITEMS = {
    "rulings": [{"ruling": "keep one module", "user_instruction": "一つにまとめて"}],
    "artifacts": [{"kind": "file", "locator": "results/table.csv"}],
}


def test_a_refused_state_write_leaves_its_history_unwritten(cur, task):
    with pytest.raises(MalformedRequestError):
        checkpoint(cur, task, goal="x" * 301, **ITEMS)

    update_task(cur, task["task"]["task_id"], actor="other", status_text="the other session won")
    with pytest.raises(StaleStateError):
        checkpoint(cur, task, status_text="still stale", **ITEMS)

    assert set(_history_counts(cur).values()) == {0}


def test_a_second_write_inside_the_interval_is_kept_with_a_warning(cur, task, task_id):
    session = uuid4()
    first = checkpoint(cur, task, status_text="measured once", session=session)
    assert "warning" not in first

    again = {**task, "state": first["state"]}
    second = checkpoint(cur, again, approach="a new approach", session=session, **ITEMS)
    assert "inside the 30-minute interval" in second["warning"]
    assert tasks.task_get(cur, task_id)["state"]["approach"] == "a new approach"
    assert set(_history_counts(cur).values()) != {0}

    other = checkpoint(
        cur, {**task, "state": second["state"]}, status_text="another session", session=uuid4()
    )
    assert "warning" not in other
    by_hand = checkpoint(cur, {**task, "state": other["state"]}, status_text="no session")
    assert "warning" not in by_hand


def test_a_zero_interval_lets_every_checkpoint_through(cur, task):
    settings.set_checkpoint_interval_minutes(cur, 0, actor="user")
    session = uuid4()
    first = checkpoint(cur, task, status_text="one", session=session)
    second = checkpoint(cur, {**task, "state": first["state"]}, status_text="two", session=session)
    assert second["state"]["status_text"] == "two"
    assert "warning" not in second


def test_the_interval_set_last_decides_and_a_store_without_it_runs_on_the_default(
    cur, task, old_store
):
    settings.set_checkpoint_interval_minutes(cur, 5, actor="user")
    session = uuid4()
    written = checkpoint(cur, task, status_text="one", session=session)
    again = checkpoint(cur, {**task, "state": written["state"]}, status_text="two", session=session)
    assert "inside the 5-minute interval" in again["warning"]
    with pytest.raises(MashuError, match="0 to 1440 minutes"):
        settings.set_checkpoint_interval_minutes(cur, 1441, actor="user")

    with db.transaction(old_store("0015_setting.sql")) as old:
        assert settings.checkpoint_interval_minutes(old) == 30


def test_a_checkpoint_renews_the_lease_and_is_refused_once_the_task_is_closed(cur, task, task_id):
    lease = "SELECT active_until, last_activity_at FROM task WHERE task_id = %s"
    expire(cur, task_id)
    before = cur.execute(lease, (task_id,)).fetchone()
    checkpoint(cur, task, rulings=[RULE])
    after = cur.execute(lease, (task_id,)).fetchone()
    assert after["active_until"] > before["active_until"]
    assert after["last_activity_at"] > before["last_activity_at"]

    closed = tasks.close(cur, task_id, outcome="completed", actor="user")
    with pytest.raises(ClosedTaskError):
        checkpoint(cur, closed, "too late")


def _rule(cur, task_id, count, *, start=0):
    """Write `count` rulings, each with words of its own, as many per write as one carries."""
    written = []
    most = task_history.CHECKPOINT_ITEM_MAX["rulings"]
    for first in range(start, start + count, most):
        items = [
            {"ruling": f"ruling {n}", "user_instruction": f"rule {n}"}
            for n in range(first, min(first + most, start + count))
        ]
        written += checkpoint(cur, tasks.task_get(cur, task_id), rulings=items)["rulings"]
    return written


def test_a_ruling_carries_the_users_words_and_comes_with_every_task_get(cur, task, task_id):
    written = checkpoint(cur, task, rulings=[{**RULE, "ruling": "  the baseline is Standard  "}])
    ruling = written["rulings"][0]
    assert (ruling["ruling"], ruling["user_instruction"]) == (
        "the baseline is Standard",
        "人間の基準を Standard にしろ",
    )

    got = task_history.expanded_task(cur, task_id)
    assert got["rulings"] == [
        {
            "ruling_id": ruling["ruling_id"],
            "on": ruling["created_at"].date(),
            "ruling": "the baseline is Standard",
            "user_said": "人間の基準を Standard にしろ",
        }
    ]
    assert "past_record" not in got
    detail = _replaced_details(cur, task_id)[-1]
    assert detail["ruling_ids"] == [str(ruling["ruling_id"])]
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'ruling_recorded'")
    assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"ruling": "judge against Hard"}, "missing required key"),
        ({"ruling": "judge against Hard", "user_instruction": "   "}, "missing required key"),
        ({"user_instruction": "Hard で"}, "missing required key"),
        ({**RULE, "reason": "agents used to write one"}, "unknown key"),
    ],
)
def test_a_ruling_without_the_users_words_is_refused(cur, task, item, message):
    with pytest.raises(MalformedRequestError, match=message):
        checkpoint(cur, task, rulings=[item])
    assert _history_counts(cur)["ruling"] == 0


def test_the_users_setting_bounds_a_rulings_length(cur, task):
    settings.set_limits(cur, {"ruling_chars": 20}, actor="user")
    with pytest.raises(MalformedRequestError) as refused:
        checkpoint(cur, task, rulings=[{**RULE, "ruling": "x" * 21}])
    assert refused.value.over_limit == [
        {"field": "rulings[0].ruling", "limit": 20, "actual": 21, "unit": "chars"}
    ]
    checkpoint(cur, task, rulings=[{**RULE, "ruling": "x" * 20}])


def test_a_task_at_the_users_cap_takes_a_ruling_only_for_one_withdrawn(cur, task, task_id):
    settings.set_limits(cur, {"ruling_count": 3}, actor="user")
    held = _rule(cur, task_id, 3)

    with pytest.raises(OverLimitError) as refused:
        _rule(cur, task_id, 1, start=3)
    answer = server._failure(refused.value)
    assert (answer["field"], answer["limit"], answer["actual"]) == ("rulings", 3, 4)
    assert "withdraw_rulings" in answer["error"]

    replaced = checkpoint(
        cur,
        tasks.task_get(cur, task_id),
        rulings=[{"ruling": "a new rule", "user_instruction": "これで行こう"}],
        withdraw_rulings=[
            {"ruling_id": str(held[0]["ruling_id"]), "user_instruction": "0 はもういい"}
        ],
    )
    assert replaced["withdrawn_rulings"][0]["ruling_id"] == held[0]["ruling_id"]
    assert task_history.ruling_count(cur, [task_id])[task_id] == 3
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'ruling_withdrawn'")
    assert cur.fetchone()["n"] == 1


def test_a_task_over_a_lowered_cap_keeps_what_it_holds_and_may_swap(cur, task, task_id):
    held = _rule(cur, task_id, 4)
    settings.set_limits(cur, {"ruling_count": 2}, actor="user")

    with pytest.raises(OverLimitError):
        _rule(cur, task_id, 1, start=4)
    checkpoint(
        cur,
        tasks.task_get(cur, task_id),
        rulings=[{"ruling": "swap", "user_instruction": "入れ替えて"}],
        withdraw_rulings=[{"ruling_id": str(held[0]["ruling_id"]), "user_instruction": "外して"}],
    )
    assert task_history.ruling_count(cur, [task_id])[task_id] == 4


def test_a_withdrawal_cannot_borrow_the_words_of_a_ruling_added_with_it(cur, task, task_id):
    held = _rule(cur, task_id, 1)[0]
    with pytest.raises(RefusedError, match="same words"):
        checkpoint(
            cur,
            tasks.task_get(cur, task_id),
            rulings=[
                {"ruling": "measure 160 battles", "user_instruction": "Hard ではかってもよい"}
            ],
            withdraw_rulings=[
                {"ruling_id": str(held["ruling_id"]), "user_instruction": "Hard ではかってもよい"}
            ],
        )
    assert task_history.ruling_count(cur, [task_id])[task_id] == 1


def test_a_withdrawal_names_a_ruling_of_this_task_still_in_force(cur, task, task_id):
    held = _rule(cur, task_id, 1)[0]
    other = new_task(cur, "different task", "history")
    withdraw = {"ruling_id": str(held["ruling_id"]), "user_instruction": "取り下げて"}

    with pytest.raises(MashuError, match="another task"):
        checkpoint(cur, other, withdraw_rulings=[withdraw])
    with pytest.raises(MashuError, match="no ruling"):
        checkpoint(
            cur,
            tasks.task_get(cur, task_id),
            withdraw_rulings=[{**withdraw, "ruling_id": str(uuid4())}],
        )
    checkpoint(cur, tasks.task_get(cur, task_id), withdraw_rulings=[withdraw])
    with pytest.raises(MashuError, match="already withdrawn"):
        checkpoint(cur, tasks.task_get(cur, task_id), withdraw_rulings=[withdraw])

    got = task_history.expanded_task(cur, task_id, withdrawn_rulings=True)
    assert got["rulings"] == []
    assert got["withdrawn_rulings"][0]["withdrawn_because"] == "取り下げて"


def test_one_checkpoint_carries_at_most_its_share_of_rulings(cur, task):
    most = task_history.CHECKPOINT_ITEM_MAX["rulings"]
    with pytest.raises(MalformedRequestError) as refused:
        checkpoint(
            cur,
            task,
            rulings=[{**RULE, "user_instruction": f"rule {n}"} for n in range(most + 1)],
            artifacts=[{"kind": "file", "locator": f"f{n}"} for n in range(5)],
        )
    assert refused.value.over_limit == [
        {"field": "rulings", "limit": most, "actual": most + 1, "unit": "items"}
    ]


def test_attempts_and_decisions_stay_as_a_record_task_get_only_counts(cur, task, task_id):
    cur.execute(
        "INSERT INTO attempt (task_id, attempt, created_by) VALUES (%s, 'tried', 'agent')",
        (task_id,),
    )
    cur.execute(
        "INSERT INTO decision (task_id, decision, created_by) VALUES (%s, '160 battles', 'agent')",
        (task_id,),
    )
    got = task_history.expanded_task(cur, task_id)
    assert got["past_record"]["attempts"] == 1 and got["past_record"]["decisions"] == 1
    assert got["past_record"]["read_with"] == f"mashu task show {task_id}"
    assert "attempts" not in got and "decisions" not in got

    shown = task_history.expanded_task(cur, task_id, attempts=True, decisions=True)
    assert [row["decision"] for row in shown["decisions"]] == ["160 battles"]


def test_rulings_are_searched_with_the_users_words_those_in_force_first(cur, task, task_id):
    held = _rule(cur, task_id, 1)[0]
    checkpoint(
        cur,
        tasks.task_get(cur, task_id),
        rulings=[{"ruling": "ruling kept", "user_instruction": "rule kept"}],
        withdraw_rulings=[{"ruling_id": str(held["ruling_id"]), "user_instruction": "drop it"}],
    )
    found = tasks.ruling_search(cur, "rule")
    assert [(row["ruling"], row["withdrawn"]) for row in found] == [
        ("ruling kept", False),
        ("ruling 0", True),
    ]
    assert found[0]["user_said"] == "rule kept"


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


def test_checkpoint_writes_rulings_and_artifacts_with_the_state(cur, task, task_id):
    first = checkpoint(
        cur,
        task,
        artifacts=[{"kind": "file", "locator": "results/table.csv", "label": "the table"}],
    )
    linked = first["artifacts"][0]
    checkpointed = checkpoint(
        cur,
        first,
        "history arrives with the checkpoint",
        status_text="checkpoint carries its history",
        rulings=[RULE],
        artifacts=[{"kind": "git_commit", "locator": "9d12f5a", "label": "fold history"}],
    )

    assert checkpointed["artifacts"][0]["label"] == "fold history"
    assert checkpointed["state"]["status_text"] == "checkpoint carries its history"
    assert checkpointed["unchecked"] is False
    assert task_history.checkpoint_list(cur, task_id) == []

    detail = _replaced_details(cur, task_id)[-1]
    assert detail["what_changed"] == "history arrives with the checkpoint"
    assert detail["ruling_ids"] == [str(checkpointed["rulings"][0]["ruling_id"])]
    assert detail["reference_ids"] == [str(checkpointed["artifacts"][0]["reference_id"])]
    assert str(linked["reference_id"]) not in detail["reference_ids"]

    expanded = task_history.expanded_task(cur, task_id, artifacts=True)
    assert len(expanded["artifacts"]) == 2
    assert expanded["artifacts"][0]["created_at"] > expanded["artifacts"][1]["created_at"]
    assert "checkpoints" not in expanded
    assert "task" in expanded and "state" in expanded


@pytest.mark.parametrize(
    ("items", "message"),
    [
        (
            {
                "rulings": [RULE, {**RULE, "user_instruction": "x" * 301}],
                "artifacts": [{"kind": "file", "locator": "src/main.py"}],
            },
            r"^rulings\[1\]\.user_instruction is 301 chars and the limit is 300",
        ),
        ({"rulings": [{**RULE, "outcome": "worked"}]}, "unknown key"),
        ({"rulings": [{"ruling": "no words given"}]}, "missing required"),
        ({"artifacts": [{"kind": "file"}]}, "missing required"),
        ({"artifacts": [{"kind": "tarball", "locator": "a.tgz"}]}, r"artifacts\[0\]\.kind"),
        (
            {"withdraw_rulings": [{"ruling_id": "not-a-uuid", "user_instruction": "drop"}]},
            "UUID",
        ),
        ({"rulings": ["just a string"]}, "must be an object"),
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
        rulings=[{"ruling": "the user's words are missing"}],
    )
    assert refused["ok"] is False
    assert [(p["field"], p["limit"]) for p in refused["over_limit"]] == [
        ("what_changed", task_history.HISTORY_LIMITS["what_changed"]),
        ("next_actions", tasks.LIST_MAX_ITEMS),
    ]
    assert len(refused["problems"]) == 3
    assert refused["problems"][2].startswith("rulings[0]")


@pytest.mark.parametrize("retired", ["attempts", "decisions"])
def test_a_checkpoint_still_carrying_attempts_or_decisions_is_refused_with_where_they_go(
    mcp, dsn, retired
):
    with db.transaction(dsn) as cur:
        new_project(cur, "history")
        made = new_task(cur, "record the task history", "history")
    refused = mcp(
        "task_checkpoint",
        task_id=str(made["task"]["task_id"]),
        expect_updated_at=made["state"]["updated_at"].isoformat(),
        status_text="should not land",
        **{retired: [{"attempt": "tried"}]},
    )
    assert refused["ok"] is False
    assert "`approach`" in refused["error"] and "`rulings`" in refused["error"]
    with db.transaction(dsn) as cur:
        assert tasks.task_get(cur, made["task"]["task_id"])["state"]["status_text"] is None


def test_a_ruling_cap_refusal_is_counted_over_mcp(mcp, dsn):
    with db.transaction(dsn) as cur:
        settings.set_limits(cur, {"ruling_count": 1}, actor="user")
        new_project(cur, "history")
        made = new_task(cur, "record the task history", "history")
        held = checkpoint(cur, made, rulings=[RULE])
    refused = mcp(
        "task_checkpoint",
        task_id=str(made["task"]["task_id"]),
        expect_updated_at=held["state"]["updated_at"].isoformat(),
        rulings=[{"ruling": "one more", "user_instruction": "これも"}],
    )
    assert (refused["ok"], refused["field"]) == (False, "rulings")
    assert refused["refusal_recorded"] is True
    with db.transaction(dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'ruling_cap_refused'")
        assert cur.fetchone()["n"] == 1


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
