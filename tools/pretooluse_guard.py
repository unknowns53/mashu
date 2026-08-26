#!/usr/bin/env python3
"""Put what the store holds in front of an action, before the action runs (30.1).

A PreToolUse hook. It reads the tool about to run, asks `mashu guard` whether
anything is pinned to the kind of judgement that tool carries out, and refuses
the call once with what came back.

Refusing rather than appending is the whole point. Section 6.1 put the read
guarantee at session start and called its own first stage a pseudo-push
depending on the agent's obedience. The failure that followed was neither
stage: the opening had fired, the index had been delivered, and the decision
came hours later with an answer already in hand from a file that is always in
context and never says it is out of date. What is in context and what is in
front of a judgement are different things.

Fires once per action per compaction generation. Every occurrence would be a
gate nobody reads, which is the failure 16.3 and 20.3 both name in their own
screens, and the second delegation of a session is taken with the first one's
answer still close. But "still close" is exactly what a compaction ends, so
the count restarts there rather than running to the end of the session id.
Every firing is logged by `mashu guard`, so how often this is right is a
question the ledger can answer rather than one to reason about.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

#: Which tools carry out which judgement. The mapping lives here rather than in
#: the store because the same judgement is reached through different tools in
#: different CLIs, and a store keyed by tool name would need a row per client.
ACTIONS = {
    "Task": "delegate",
    "Agent": "delegate",
    "mcp__codex-async__codex_start": "delegate",
}

MARKERS = pathlib.Path(tempfile.gettempdir()) / "mashu-guard"

#: What a completed compaction leaves in the transcript. Matched as a raw
#: substring: the line is one JSON object per transcript entry and this key is
#: written without spaces, so parsing every line to find it would cost the
#: whole file for one boolean.
COMPACTED = '"isCompactSummary":true'


def generation(transcript: str | None) -> int:
    """How many times this session has been compacted.

    Firing once per session assumes the first firing is still in the context
    when the second decision arrives. Compaction is exactly where that stops
    being true: the session id does not change, so the marker survives, while
    the conversation the guard wrote into is dropped. The gate then stays shut
    over a context that no longer holds what it said, which is the failure
    this hook exists to prevent, reintroduced by its own bookkeeping.

    Counting compactions turns the marker into one per generation. A session
    that has never been compacted is generation 0 and behaves as before.
    """
    if not transcript:
        return 0
    try:
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            return sum(1 for line in handle if COMPACTED in line)
    except OSError:
        # An unreadable transcript is not evidence that nothing was dropped,
        # but neither is it grounds to fire on every call. Hold generation 0
        # and behave as this hook did before.
        return 0


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    action = ACTIONS.get(event.get("tool_name", ""))
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
        # A guard that cannot run does not block the work. This is the one
        # place that judgement is made, and it is made this way because the
        # store being unreachable is not evidence about the decision at hand.
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
