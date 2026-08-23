"""What must never enter the store (redact).

The store is read into every session and travels outward from there, so an
identifier that lands in it has been published slowly rather than not at all.
Review will not catch it: review reads for whether a claim is true.
"""

from __future__ import annotations

import pytest

from mashu import redact


@pytest.fixture
def patterns(tmp_path, monkeypatch):
    def _write(*lines):
        path = tmp_path / "banned"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv(redact.PATTERNS_ENV_VAR, str(path))
        return path

    return _write


def test_ordinary_content_passes(patterns):
    patterns("someone", "somewhere-university")
    assert redact.check("測定の前に何を決めるかを書き出す").allowed


def test_a_match_anywhere_in_any_field_refuses(patterns):
    patterns("someone")
    verdict = redact.check("題", "本文に /home/someone/notes が入っている")
    assert not verdict.allowed
    assert verdict.pattern_index == 0


def test_the_matched_text_is_never_reported_back(patterns):
    """A refusal that quotes the identifier copies it into a log and a terminal."""
    patterns("someone")
    verdict = redact.check("/home/someone/notes")
    assert "someone" not in verdict.reason()
    assert "someone" not in repr(verdict)


def test_a_missing_list_is_reported_as_unchecked_not_as_clean(monkeypatch):
    """Silence here would be indistinguishable from a guard that ran and passed."""
    monkeypatch.setenv(redact.PATTERNS_ENV_VAR, "/nonexistent/list")
    verdict = redact.check("なんでも")
    assert verdict.allowed and verdict.unchecked
    assert "not found" in verdict.reason()


def test_one_malformed_line_does_not_disarm_the_rest(patterns):
    patterns("someone", "[unclosed", "somewhere-university")
    assert not redact.check("somewhere-university に所属").allowed


def test_comments_and_blank_lines_are_not_patterns(patterns):
    patterns("# a comment", "", "someone")
    assert redact.check("a comment だけの本文").allowed
    assert not redact.check("someone").allowed


def test_matching_ignores_case(patterns):
    patterns("someone")
    assert not redact.check("SomeOne").allowed
