#!/usr/bin/env python3
"""Check tool actions against configured guard rules."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

#: Which judgement a tool carries out, for the tools the client itself ships.
ACTIONS = {
    "Task": "delegate",
    "Agent": "delegate",
}

#: External tools and wrappers are configured per installation.
ACTIONS_FILE = ".mashu-guard-actions"
ACTIONS_ENV_VAR = "MASHU_GUARD_ACTIONS"

#: A tool is matched whole, a command searched.
SUBJECTS = ("tool", "command")


def actions_path() -> pathlib.Path | None:
    override = os.environ.get(ACTIONS_ENV_VAR)
    if override:
        path = pathlib.Path(override).expanduser()
        return path if path.exists() else None
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / ACTIONS_FILE
        if candidate.exists():
            return candidate
    return None


def rules() -> list[tuple[str, str, str]]:
    """The configured rules as (subject, expression, judgement), in reading order."""
    path = actions_path()
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[tuple[str, str, str]] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        named, _, rest = line.partition(" ")
        subject, _, expression = rest.strip().partition(" ")
        expression = expression.strip()
        if subject not in SUBJECTS or not expression:
            continue
        if subject == "command":
            try:
                re.compile(expression)
            except re.error:
                continue
        out.append((subject, expression, named))
    return out


MARKERS = pathlib.Path(tempfile.gettempdir()) / "mashu-guard"

#: What a completed compaction leaves in the transcript.
COMPACTED = '"isCompactSummary":true'


def generation(transcript: str | None) -> int:
    """How many times this session has been compacted."""
    if not transcript:
        return 0
    try:
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            return sum(1 for line in handle if COMPACTED in line)
    except OSError:
        # If the transcript cannot be read, preserve the previous generation.
        return 0


def action_for(event: dict) -> str | None:
    """The judgement this call carries out, or nothing if it carries none."""
    tool = event.get("tool_name", "")
    configured = rules()
    if tool:
        if tool in ACTIONS:
            return ACTIONS[tool]
        for subject, expression, named in configured:
            if subject == "tool" and expression == tool:
                return named
    command = (event.get("tool_input") or {}).get("command") or ""
    if not command:
        return None
    for subject, expression, named in configured:
        if subject == "command" and re.search(expression, command):
            return named
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    action = action_for(event)
    if not action:
        return 0

    session = str(event.get("session_id") or "unknown")
    marker = MARKERS / f"{session}.{action}.{generation(event.get('transcript_path'))}"
    if marker.exists():
        return 0

    try:
        done = subprocess.run(
            ["mashu", "guard", action, "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        # A guard that cannot run does not block the work.
        return 0

    if done.returncode != 2 or not done.stdout.strip():
        return 0

    try:
        pinned = json.loads(done.stdout)
    except json.JSONDecodeError:
        return 0

    MARKERS.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")

    lines = [f"Mashu holds this about {action}. Read it, then decide again.", ""]
    for row in pinned:
        lines.append(row["content"])
        lines.append("")
    lines.append(
        "If it still points the same way, make the same call again and it will go through."
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "\n".join(lines),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
