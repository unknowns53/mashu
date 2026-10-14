from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from mashu import cli, compactions

TASK, OTHER = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
CHECKPOINT, GET = "mcp__mashu__task_checkpoint", "mcp__mashu__task_get"


def call(name, task_id=None, **entry):
    given = {"task_id": task_id} if task_id else {}
    part = {"type": "tool_use", "name": name, "input": given}
    return {"type": "assistant", "message": {"content": [part]}, **entry}


def boundary(uuid, *, trigger="manual", days_ago=1):
    at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    meta = {"trigger": trigger}
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "timestamp": at,
        "cwd": "/work/enrai",
        "compactMetadata": meta,
    }


def transcript(path, *entries, raw=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry) for entry in entries] + list(raw)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_compaction_measures_the_checkpoint_before_and_the_read_after(tmp_path):
    path = transcript(
        tmp_path / "p" / "a.jsonl",
        call("Read"),
        call(CHECKPOINT, TASK),
        call("Edit"),
        call("Bash"),
        call("Agent", isSidechain=True),
        call("Edit", isSidechain=True),
        boundary("b1"),
        call("Write"),
        call(GET, OTHER),
        call("mcp__plugin_mashu_mashu__task_get", TASK),
        call("Edit"),
    )

    [row] = compactions.scan([path]).compactions

    assert (row.task_id, row.calls_before, row.project) == (TASK, 4, "enrai")
    assert (row.calls_since_checkpoint, row.edits_since_checkpoint) == (2, 1)
    assert (row.calls_after, row.calls_before_retrieval, row.edits_before_retrieval) == (4, 2, 1)
    assert row.read_other_task


def test_each_compaction_counts_only_the_window_since_the_one_before(tmp_path):
    path = transcript(
        tmp_path / "p" / "a.jsonl",
        call(CHECKPOINT, TASK),
        boundary("b1"),
        call("Edit"),
        boundary("b2", days_ago=0),
        call(GET, TASK),
    )

    first, second = compactions.scan([path]).compactions

    assert (first.calls_after, first.calls_before_retrieval) == (1, None)
    # The second window kept working on the task without checkpointing it again.
    assert (second.task_id, second.calls_before, second.calls_since_checkpoint) == (TASK, 1, None)
    assert second.calls_before_retrieval == 0


def test_a_window_that_touched_no_task_is_measured_without_one(tmp_path):
    path = transcript(tmp_path / "p" / "a.jsonl", call("Edit"), boundary("b1"), call(GET, TASK))

    [row] = compactions.scan([path]).compactions

    assert (row.task_id, row.calls_before_retrieval, row.read_other_task) == (None, None, False)


def test_a_copied_boundary_is_counted_once_where_the_work_went_on(tmp_path):
    head = (call(CHECKPOINT, TASK), boundary("b1"))
    original = transcript(tmp_path / "p" / "a.jsonl", *head, call("Edit"))
    resumed = transcript(tmp_path / "p" / "b.jsonl", *head, call("Edit"), call(GET, TASK))

    report = compactions.scan([original, resumed])

    [row] = report.compactions
    assert (report.transcripts, row.calls_after, row.calls_before_retrieval) == (2, 2, 1)


def test_unparsable_lines_are_counted_and_old_compactions_left_out(tmp_path):
    path = transcript(
        tmp_path / "p" / "a.jsonl",
        boundary("old", days_ago=40),
        boundary("new", days_ago=2),
        raw=("{not json",),
    )

    report = compactions.scan([path], days=30)

    assert report.unreadable_lines == 1
    assert [row.uuid for row in report.compactions] == ["new"]


@pytest.mark.parametrize(
    "name,verb",
    [
        (GET, "task_get"),
        ("mcp__plugin_mashu_mashu__task_checkpoint", "task_checkpoint"),
        ("mcp__other__task_get", None),
        ("Edit", None),
    ],
)
def test_a_mashu_call_is_known_under_any_server_name(name, verb):
    assert compactions.mashu_verb(name) == verb


def test_the_command_reports_capture_and_retrieval_and_lists_the_lagging(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    transcript(
        tmp_path / "projects" / "p" / "a.jsonl",
        call(CHECKPOINT, TASK),
        boundary("kept"),
        call(GET, TASK),
        call(CHECKPOINT, TASK),
        call("Edit"),
        boundary("lagged", trigger="auto", days_ago=0),
        call("Edit"),
        call(GET, OTHER),
    )

    assert cli.main(["compactions", "--list"]) == 0
    out = capsys.readouterr().out.splitlines()

    assert out[0] == "compactions  last_30_days=2  manual=1  auto=1  on_a_task=2  transcripts=1"
    assert out[1].startswith("capture    manual  checkpointed=1/1  calls_since median=0")
    assert out[2].startswith("capture    auto  checkpointed=1/1  calls_since median=1")
    assert out[2].endswith("edited_since=1")
    assert out[3] == (
        "retrieval  read_same_task=1/2  calls_before median=0  p75=0  edited_before=1"
    )
    assert out[4] == "retrieval  not_read  other_task_read=1  none_read=0"
    assert len(out) == 7 and " auto " in out[6]
