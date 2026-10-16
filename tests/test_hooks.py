from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pretooluse_guard  # noqa: E402
import sessionstart_guard  # noqa: E402


def git(repo, *args, **kwargs):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **kwargs)


@pytest.fixture
def hook_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "core.hooksPath", str(ROOT / "hooks"))
    (tmp_path / ".git-banned-patterns").write_text("SECRET\n")
    return tmp_path


def hook(repo, name, *args):
    # Run as a commit runs it, through the shell git brings, which Windows needs.
    argv = ["git", "hook", "run", name, "--", *map(str, args)]
    return subprocess.run(argv, cwd=repo, capture_output=True, encoding="utf-8")


@pytest.mark.parametrize("name", ["pre-commit", "commit-msg"])
def test_invalid_banned_pattern_rejects_commit(hook_repo, name):
    (hook_repo / ".git-banned-patterns").write_text("SECRET\n[\n")
    (hook_repo / "file.txt").write_text("SECRET\n")
    git(hook_repo, "add", "file.txt")
    (hook_repo / "message").write_text("SECRET\n")
    result = hook(hook_repo, name, *(["message"] if name == "commit-msg" else []))
    assert result.returncode != 0
    assert "cannot check banned patterns" in result.stderr


@pytest.mark.parametrize("pattern,content", [("日本語", "safe"), ("SECRET", "SECRET")])
def test_staged_filename_is_decoded_before_path_and_blob_checks(hook_repo, pattern, content):
    (hook_repo / ".git-banned-patterns").write_text(pattern + "\n", encoding="utf-8")
    blob = git(hook_repo, "hash-object", "-w", "--stdin", input=content, text=True).stdout.strip()
    git(hook_repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},日本語.txt")
    result = hook(hook_repo, "pre-commit")
    assert result.returncode != 0
    assert "banned pattern" in result.stderr


def test_linked_worktree_reads_main_checkout_patterns(hook_repo):
    git(hook_repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", "root")
    worktree = hook_repo / "linked"
    git(hook_repo, "worktree", "add", "-q", str(worktree))
    (worktree / "message").write_text("SECRET\n")
    result = hook(worktree, "commit-msg", "message")
    assert result.returncode != 0
    assert "banned pattern" in result.stderr


def test_pretooluse_uses_local_mashu_and_separates_option_like_action(monkeypatch, tmp_path):
    event = {"tool_name": "External", "session_id": "test"}
    monkeypatch.setattr(pretooluse_guard, "rules", lambda: [("tool", "External", "-guard")])
    monkeypatch.setattr(pretooluse_guard, "MARKERS", tmp_path / "markers")
    monkeypatch.setattr(pretooluse_guard.sys, "stdin", io.StringIO(json.dumps(event)))
    seen = []

    def run(argv, **kw):
        return seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pretooluse_guard.subprocess, "run", run)
    assert pretooluse_guard.main() == 0
    assert seen == [[str(sessionstart_guard.MASHU), "guard", "--json", "--", "-guard"]]
    monkeypatch.setattr(sessionstart_guard, "MASHU", tmp_path / "absent")
    monkeypatch.setattr(pretooluse_guard.sys, "stdin", io.StringIO(json.dumps(event)))
    assert pretooluse_guard.main() == 0
    assert seen[-1] == ["mashu", "guard", "--json", "--", "-guard"]


DENIAL = [{"memory_id": "m", "content": "hand over the goal, not the steps", "topic": "handover"}]


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    """Send one delegate call through the hook, and hand back its output and the CLI calls."""
    monkeypatch.setattr(pretooluse_guard, "rules", lambda: [])
    monkeypatch.setattr(pretooluse_guard, "MARKERS", tmp_path / "markers")
    seen = []

    def call(tool_input, *, cli=None):
        def run(argv, **kw):
            seen.append(argv[1:])
            if cli is not None:
                return cli(argv)
            if argv[1:3] == ["guard", "--json"]:
                return subprocess.CompletedProcess(argv, 2, json.dumps(DENIAL), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(pretooluse_guard.subprocess, "run", run)
        event = {"tool_name": "Agent", "session_id": "s1", "tool_input": tool_input}
        monkeypatch.setattr(pretooluse_guard.sys, "stdin", io.StringIO(json.dumps(event)))
        out = io.StringIO()
        monkeypatch.setattr(pretooluse_guard.sys, "stdout", out)
        assert pretooluse_guard.main() == 0
        return out.getvalue()

    return call, seen, tmp_path / "markers" / "s1.delegate.0"


def test_the_first_call_carries_the_rules_as_a_warning_and_later_calls_pass_silently(guarded):
    call, seen, marker = guarded
    out = json.loads(call({"description": "port", "prompt": "do it"}))["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert "hand over the goal, not the steps" in out["additionalContext"]
    assert json.loads(marker.read_text(encoding="utf-8")) == {"topic": "handover"}

    seen.clear()
    assert call({"description": "port", "prompt": "do it, keeping the goal"}) == ""
    assert seen == []


def test_a_marker_written_before_outcomes_were_kept_stays_silent(guarded):
    call, seen, marker = guarded
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")
    assert call({"prompt": "do it"}) == "" and call({"prompt": "do it"}) == ""
    assert seen == []


def test_status_shows_how_often_each_linked_topic_changed_the_next_call(committing_dsn, capsys):
    from uuid import uuid4

    from mashu import cli

    def run(*argv):
        code = cli.main(["--dsn", committing_dsn, *argv])
        return code, capsys.readouterr().out

    name, action = f"handover-{uuid4().hex[:8]}", f"act{uuid4().hex[:8]}"
    run("topic", "--add", name, "--trigger", "Before handing work to a subagent")
    run("remember", "name the goal and the finish line when handing work over", "--topic", name)
    run("topic", "--edit", name, "--action", action)
    line = f"guard   {name} ({action})"
    assert f"{line}  served_30d=0  followed=0  changed=-" in run("status")[1]

    for _ in range(3):
        assert run("guard", action, "--json")[0] == 2
    for outcome in ("same", "changed", "same", "same"):
        assert run("guard", "--outcome", outcome, "--topic", name, "--", action) == (0, "")
    assert f"{line}  served_30d=3  followed=4  changed=25%" in run("status")[1]

    assert cli.main(["--dsn", committing_dsn, "guard", "--topic", name, action]) == 1
    assert "--outcome" in capsys.readouterr().err


@pytest.fixture
def delivered(monkeypatch):
    """Run the SessionStart hook for one event, and hand back its output and CLI calls."""

    def fire(event, *, stdout="always\n  rule\n", returncode=0):
        seen = []

        def run(argv, **kw):
            seen.append((argv[1:], kw.get("cwd")))
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        monkeypatch.setattr(sessionstart_guard.subprocess, "run", run)
        monkeypatch.setattr(sessionstart_guard.sys, "stdin", io.StringIO(json.dumps(event)))
        out = io.StringIO()
        monkeypatch.setattr(sessionstart_guard.sys, "stdout", out)
        assert sessionstart_guard.main() == 0
        return out.getvalue(), seen

    return fire


def test_a_compaction_redelivers_the_bootstrap_under_the_redelivery_preamble(delivered, tmp_path):
    out, seen = delivered({"source": "compact", "cwd": str(tmp_path)})

    assert out.startswith(sessionstart_guard.REDELIVERY + "\n\n")
    assert out.endswith("always\n  rule\n")
    assert seen == [(["bootstrap"], str(tmp_path))]


@pytest.mark.parametrize("source", ["startup", "resume", "", None])
def test_every_other_start_uses_the_startup_preamble(delivered, source):
    out, _ = delivered({"source": source})

    assert out.startswith(sessionstart_guard.DELIVERY + "\n\n")


@pytest.mark.parametrize("stdout,returncode", [("", 0), ("rule\n", 1)])
def test_a_failed_bootstrap_leaves_the_session_without_output(delivered, stdout, returncode):
    out, _ = delivered({"source": "compact"}, stdout=stdout, returncode=returncode)

    assert out == ""


def test_a_compaction_hands_its_transcript_over_so_the_resumed_task_is_named(delivered, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    named = ["bootstrap", "--resume-transcript", str(transcript)]

    _, seen = delivered({"source": "compact", "transcript_path": str(transcript)})
    assert seen == [(named, None)]
    for event in (
        {"source": "startup", "transcript_path": str(transcript)},
        {"source": "compact", "transcript_path": str(tmp_path / "missing.jsonl")},
        {"source": "compact"},
    ):
        _, seen = delivered(event)
        assert seen == [(["bootstrap"], None)]
