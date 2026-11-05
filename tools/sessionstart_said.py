#!/usr/bin/env python3
"""Give a compacted session back the messages its user wrote before the compaction.

Claude Code's summary lists the user's messages of the window it summarizes, and each later
summary is written from the one before it, so a correction made two compactions ago survives
only if every summary in between copied it. The transcript keeps every message, so this hook
reads them from there instead of relying on the summaries or on Mashu writes.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime

PREAMBLE = (
    "Mashu: 圧縮の前にこのセッションでユーザーが書いた発言と選択の回答を、古い順に再掲する。"
    "要約に残っていない訂正や却下も、後の発言で覆されていない限り今も有効である。"
)

#: Claude Code replaces hook stdout longer than 10,000 characters with a 2,000-character preview,
#: so the whole output stays below that with room for the preamble.
BUDGET = 9_000

#: A long paste would otherwise crowd out the corrections around it.
PER_MESSAGE = 300

QUESTION = 60


def _clip(text: str, limit: int) -> str:
    text = " / ".join(line.strip() for line in text.strip().splitlines() if line.strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stamp(value: object) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "--"
    return moment.astimezone().strftime("%m-%d %H:%M")


def said(path: pathlib.Path) -> list[str]:
    """One line per message the user typed and per question they answered, oldest first."""
    lines: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            if entry.get("type") != "user":
                continue
            # Task notifications, hook feedback and other sessions' messages also arrive as
            # user turns; only the typed ones carry this origin.
            origin = entry.get("origin") or {}
            content = (entry.get("message") or {}).get("content")
            text = ""
            if origin.get("kind") == "human":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                    text = "\n".join(part for part in parts if part)
                text = _clip(text, PER_MESSAGE)
            else:
                answered = entry.get("toolUseResult")
                answers = answered.get("answers") if isinstance(answered, dict) else None
                if isinstance(answers, dict) and answers:
                    text = _clip(
                        "; ".join(
                            f"選択「{_clip(str(q), QUESTION)}」→ {a}" for q, a in answers.items()
                        ),
                        PER_MESSAGE,
                    )
            if not text:
                continue
            line = f"{_stamp(entry.get('timestamp'))}  {text}"
            # A resumed transcript and a resent prompt both repeat a message verbatim.
            if lines and lines[-1].split("  ", 1)[1] == text:
                continue
            lines.append(line)
    return lines


def render(lines: list[str]) -> str:
    """The preamble and the newest lines that fit the budget."""
    kept: list[str] = []
    used = len(PREAMBLE) + 40
    for line in reversed(lines):
        if used + len(line) + 1 > BUDGET:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    head = [PREAMBLE, ""]
    dropped = len(lines) - len(kept)
    if dropped:
        head.append(f"（字数の上限のため、古い {dropped} 件は省いた）")
    return "\n".join(head + kept)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(event, dict) or event.get("source") != "compact":
        return 0
    transcript = event.get("transcript_path")
    if not transcript or not pathlib.Path(transcript).is_file():
        return 0
    try:
        lines = said(pathlib.Path(transcript))
    except OSError:
        return 0
    if lines:
        print(render(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
