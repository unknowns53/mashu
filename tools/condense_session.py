"""Condense a Claude Code transcript into something a prompt can hold.

Written for the offline extraction check in specification 27.4. A raw
transcript is a few megabytes of tool payloads around a much smaller
conversation; the extraction prompt only needs what was said and what was done,
not the bytes that flowed through.

Usage:
    python tools/condense_session.py TRANSCRIPT.jsonl > condensed.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

#: Tool arguments worth keeping, in the order they are tried.
TOOL_SUMMARY_KEYS = (
    "description",
    "command",
    "file_path",
    "pattern",
    "query",
    "prompt",
    "url",
)

TOOL_ARG_LIMIT = 160
RESULT_LIMIT = 200


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _summarise_tool(block: dict) -> str:
    name = block.get("name", "tool")
    args = block.get("input") or {}
    for key in TOOL_SUMMARY_KEYS:
        if args.get(key):
            return f"[{name}] {_clip(args[key], TOOL_ARG_LIMIT)}"
    return f"[{name}]"


def _render_user(record: dict) -> str | None:
    if record.get("isMeta"):
        return None
    parts: list[str] = []
    for block in _blocks(record.get("message") or {}):
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_result":
            # Only the shape of the result matters here, and whether it failed.
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
            marker = "error" if block.get("is_error") else "result"
            parts.append(f"    ({marker}: {_clip(body or '', RESULT_LIMIT)})")
    text = "\n".join(p for p in parts if p.strip())
    return text or None


def _render_assistant(record: dict) -> str | None:
    parts: list[str] = []
    for block in _blocks(record.get("message") or {}):
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            parts.append("    " + _summarise_tool(block))
        # thinking is dropped: it is the largest part of a transcript and the
        # extraction judges what was concluded, not how it was reached.
    text = "\n".join(p for p in parts if p.strip())
    return text or None


def condense(path: pathlib.Path) -> str:
    out: list[str] = []
    title = None
    turns = 0

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = record.get("type")
            if kind == "ai-title" and not title:
                title = record.get("aiTitle")
            if record.get("isSidechain"):
                continue  # subagent transcripts have their own files

            if kind == "user":
                text = _render_user(record)
                if text:
                    out.append(f"\n## user\n\n{text}")
                    turns += 1
            elif kind == "assistant":
                text = _render_assistant(record)
                if text:
                    out.append(f"\n## assistant\n\n{text}")
                    turns += 1

    header = [
        f"# session {path.stem}",
        "",
        f"- title: {title or '(none recorded)'}",
        f"- turns kept: {turns}",
        f"- source: {path}",
    ]
    return "\n".join(header) + "\n" + "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=pathlib.Path)
    parser.add_argument("-o", "--output", type=pathlib.Path)
    args = parser.parse_args()

    text = condense(args.transcript)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(
            f"{args.transcript.stat().st_size / 1e6:.1f} MB -> "
            f"{len(text.encode()) / 1e3:.0f} kB  {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
