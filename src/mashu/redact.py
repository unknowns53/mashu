"""What must never enter the knowledge state, whoever wrote it.

The store is not a private notebook. Its contents are handed to agents on
every session and travel outward from there into whatever those agents
produce, so a personal identifier that lands here has been published slowly
rather than not at all. Nothing downstream is going to catch it: review reads
for whether a claim is true, not for whether a home directory is spelled out
in the middle of a path.

The patterns are held outside the repository — the same file the git hooks
read — because a list of the things that must not be committed is itself a
list of those things. When the file is absent the check does not silently
pass: it reports that it could not run, and the caller decides.

Deliberately not a redactor. It refuses rather than rewrites. Editing content
on its way in would leave text nobody chose, which is exactly the provenance
this layer exists to keep honest.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass

#: The gitignored file the commit hooks already use.
PATTERNS_FILE = ".git-banned-patterns"
PATTERNS_ENV_VAR = "MASHU_BANNED_PATTERNS"


@dataclass(frozen=True)
class Verdict:
    """Whether content may be written, and whether the question could be asked."""

    #: False when a pattern matched.
    allowed: bool
    #: Index of the pattern that matched, never the text it matched. Reporting
    #: the match would copy the identifier into a log, an exception message and
    #: a terminal, which is three more places than it was in.
    pattern_index: int | None = None
    #: True when no pattern file was found. The content is allowed through, and
    #: the caller is told the check did not run rather than told it passed.
    unchecked: bool = False

    def reason(self) -> str:
        if self.unchecked:
            return "the banned-pattern list was not found, so nothing was checked"
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


def load() -> list[re.Pattern[str]] | None:
    """The compiled list, or nothing when the file is absent."""
    path = patterns_path()
    if path is None:
        return None
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line, re.IGNORECASE))
        except re.error:
            # A malformed line is a broken guard, not a reason to stop guarding
            # with the rest of the list.
            continue
    return out


def check(*texts: str | None) -> Verdict:
    """Whether these pieces of text may be written."""
    compiled = load()
    if compiled is None:
        return Verdict(allowed=True, unchecked=True)
    for text in texts:
        if not text:
            continue
        for index, pattern in enumerate(compiled):
            if pattern.search(text):
                return Verdict(allowed=False, pattern_index=index)
    return Verdict(allowed=True)
