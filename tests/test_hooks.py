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
    # No Windows filesystem holds this name, so it goes only into the index the hook reads,
    # which Git for Windows also refuses while it protects NTFS.
    git(hook_repo, "config", "core.protectNTFS", "false")
    blob = git(hook_repo, "hash-object", "-w", "--stdin", input=content, text=True).stdout.strip()
    git(hook_repo, "update-index", "--add", "--cacheinfo", f'100644,{blob},日本語"\nfile.txt')
    result = hook(hook_repo, "pre-commit")
    assert result.returncode != 0
    assert "banned pattern" in result.stderr


def test_unreadable_staged_blob_rejects_commit(hook_repo):
    git(hook_repo, "update-index", "--add", "--cacheinfo", "100644," + "f" * 40 + ",lost.txt")
    result = hook(hook_repo, "pre-commit")
    assert result.returncode != 0
    assert "cannot read staged blob" in result.stderr


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
