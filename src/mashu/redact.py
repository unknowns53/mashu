"""Check input text against tool-call markup and the configured banned patterns."""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass

#: The gitignored file the commit hooks already use.
PATTERNS_FILE = ".git-banned-patterns"
PATTERNS_ENV_VAR = "MASHU_BANNED_PATTERNS"

#: An agent's own tool-call syntax. When a model loses track of where one argument ends,
#: the next argument's opening tag lands inside the text of the one before it.
CALL_MARKUP = re.compile(r"</?(?:antml:)?(?:parameter|invoke|function_calls)\b")


@dataclass(frozen=True)
class Verdict:
    """Whether content may be written, and whether the question could be asked."""

    #: False when a pattern matched.
    allowed: bool
    #: Index of the pattern that matched, never the text it matched.
    pattern_index: int | None = None
    #: True when the pattern file could not be read.
    unchecked: bool = False
    #: How many lines of the list would not compile.
    malformed: int = 0
    #: True when the text carries tool-call markup.
    call_markup: bool = False

    def reason(self) -> str:
        if self.call_markup:
            return (
                "contains tool-call markup such as <parameter name=...>, so one argument has "
                "swallowed the next; send each field as its own argument"
            )
        if self.unchecked:
            return "the banned-pattern list was unavailable, so nothing was checked"
        if self.allowed:
            return "no banned pattern"
        return f"matches banned pattern #{self.pattern_index}"


def patterns_path() -> pathlib.Path | None:
    override = os.environ.get(PATTERNS_ENV_VAR)
    if override:
        path = pathlib.Path(override).expanduser()
        return path if path.exists() else None
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / PATTERNS_FILE
        if candidate.exists():
            return candidate
    return None


def load() -> tuple[list[re.Pattern[str]], int] | None:
    """The compiled list and the count of lines that would not compile."""
    path = patterns_path()
    if path is None:
        return None
    out = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line, re.IGNORECASE))
        except re.error:
            # A malformed line is a broken guard, not a reason to stop guarding with the rest of
            # the list.
            malformed += 1
            continue
    return out, malformed


def check(*texts: str | None) -> Verdict:
    """Whether these pieces of text may be written."""
    if any(text and CALL_MARKUP.search(text) for text in texts):
        return Verdict(allowed=False, call_markup=True)
    loaded = load()
    if loaded is None:
        return Verdict(allowed=True, unchecked=True)
    compiled, malformed = loaded
    for text in texts:
        if not text:
            continue
        for index, pattern in enumerate(compiled):
            if pattern.search(text):
                return Verdict(allowed=False, pattern_index=index, malformed=malformed)
    return Verdict(allowed=True, malformed=malformed)
