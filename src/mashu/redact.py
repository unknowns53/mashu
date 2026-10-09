"""Check input text against tool-call markup and the configured banned patterns."""

from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from mashu.errors import RefusedError

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
    #: The caller's label for the text that matched, such as `artifacts[0].locator`.
    field: str | None = None
    #: Where the match sits in that text, as a slice (start inclusive, end exclusive).
    span: tuple[int, int] | None = None

    def reason(self) -> str:
        where = f" at characters {self.span[0]}-{self.span[1]}" if self.span else ""
        subject = f"{self.field} " if self.field else ""
        if self.call_markup:
            return (
                f"{subject}contains tool-call markup such as <parameter name=...>{where}, so "
                "one argument has swallowed the next; send each field as its own argument"
            )
        if self.unchecked:
            return "the banned-pattern list was unavailable, so nothing was checked"
        if self.allowed:
            return "no banned pattern"
        return (
            f"{subject}matches banned pattern #{self.pattern_index}{where}; "
            "rewrite that part and resend"
        )

    def refusal(self) -> RefusedError:
        return RefusedError(self.reason(), field=self.field, span=self.span)


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


def items(name: str, texts: Iterable[str | None] | None) -> dict[str, str | None]:
    """Label each text of a list argument by its index, as `name[0]`, `name[1]`, ..."""
    return {f"{name}[{index}]": text for index, text in enumerate(texts or [])}


def check(fields: Mapping[str, str | None]) -> Verdict:
    """Whether these texts may be written, each keyed by the argument name a caller sees."""
    present = [(label, text) for label, text in fields.items() if text]
    for label, text in present:
        markup = CALL_MARKUP.search(text)
        if markup:
            return Verdict(allowed=False, call_markup=True, field=label, span=markup.span())
    loaded = load()
    if loaded is None:
        return Verdict(allowed=True, unchecked=True)
    compiled, malformed = loaded
    for label, text in present:
        for index, pattern in enumerate(compiled):
            hit = pattern.search(text)
            if hit:
                return Verdict(
                    allowed=False,
                    pattern_index=index,
                    malformed=malformed,
                    field=label,
                    span=hit.span(),
                )
    return Verdict(allowed=True, malformed=malformed)


def gate(fields: Mapping[str, str | None]) -> Verdict:
    """Check these texts and raise the refusal when one may not be written."""
    verdict = check(fields)
    if not verdict.allowed:
        raise verdict.refusal()
    return verdict
