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


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def hook_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".git-banned-patterns").write_text("SECRET\n")
    return tmp_path


def hook(repo, name, *args):
    return subprocess.run(
        [str(ROOT / "hooks" / name), *map(str, args)], cwd=repo, capture_output=True, text=True
    )


@pytest.mark.parametrize("name", ["pre-commit", "commit-msg"])
def test_invalid_banned_pattern_rejects_commit(hook_repo, name):
    (hook_repo / ".git-banned-patterns").write_text("SECRET\n[\n")
    (hook_repo / "file.txt").write_text("SECRET\n")
    git(hook_repo, "add", "file.txt")
    message = hook_repo / "message"
    message.write_text("SECRET\n")
    result = hook(hook_repo, name, message) if name == "commit-msg" else hook(hook_repo, name)
    assert result.returncode != 0
    assert "cannot check banned patterns" in result.stderr


@pytest.mark.parametrize("pattern,content", [("日本語", "safe"), ("SECRET", "SECRET")])
def test_staged_filename_is_decoded_before_path_and_blob_checks(hook_repo, pattern, content):
    (hook_repo / ".git-banned-patterns").write_text(pattern + "\n")
    name = '日本語"\nfile.txt'
    (hook_repo / name).write_text(content)
    git(hook_repo, "add", name)
    result = hook(hook_repo, "pre-commit")
    assert result.returncode != 0
    assert "banned pattern" in result.stderr


def test_unreadable_staged_blob_rejects_commit(hook_repo):
    git(hook_repo, "update-index", "--add", "--cacheinfo", "100644," + "f" * 40 + ",lost.txt")
    result = hook(hook_repo, "pre-commit")
    assert result.returncode != 0
    assert "cannot read staged blob" in result.stderr


@pytest.mark.parametrize("removed", ["tests/a.py", "tests/line-budget"])
def test_deletion_only_commit_checks_line_budget(hook_repo, removed):
    tests = hook_repo / "tests"
    tests.mkdir()
    (tests / "a.py").write_text("pass\n")
    (tests / "line-budget").write_text("1\n")
    git(hook_repo, "add", "tests")
    git(hook_repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    (hook_repo / removed).unlink()
    git(hook_repo, "add", "-u")
    result = hook(hook_repo, "pre-commit")
    assert result.returncode != 0
    assert "tests/line-budget" in result.stderr


def test_pretooluse_uses_local_mashu_and_separates_option_like_action(monkeypatch, tmp_path):
    event = {"tool_name": "External", "session_id": "test"}
    monkeypatch.setattr(pretooluse_guard, "rules", lambda: [("tool", "External", "-guard")])
    monkeypatch.setattr(pretooluse_guard, "MARKERS", tmp_path / "markers")
    monkeypatch.setattr(pretooluse_guard.sys, "stdin", io.StringIO(json.dumps(event)))
    seen = []
    monkeypatch.setattr(
        pretooluse_guard.subprocess,
        "run",
        lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    assert pretooluse_guard.main() == 0
    assert seen == [[str(ROOT / ".venv" / "bin" / "mashu"), "guard", "--json", "--", "-guard"]]
    monkeypatch.setattr(sessionstart_guard, "MASHU", tmp_path / "absent")
    monkeypatch.setattr(pretooluse_guard.sys, "stdin", io.StringIO(json.dumps(event)))
    assert pretooluse_guard.main() == 0
    assert seen[-1] == ["mashu", "guard", "--json", "--", "-guard"]
