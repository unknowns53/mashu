#!/usr/bin/env python3
"""Hand the opening to a session, from the harness rather than from the model.

A SessionStart hook. It runs on `startup` and `resume`, where it *is* the
delivery, and on `compact`, where it is a re-delivery of what the compaction
dropped.

Why the harness and not the model. The calling discipline lives in the MCP
server's instructions, and the protocol is weaker there than it reads: it
specifies that `instructions` reach the client, not that the client puts them
in front of the model. Claude Code does use them and truncates them at 2 KB;
Codex has an open issue where they do not reliably act as agent guidance. For
`trace_put` and `pain_report` that is survivable — a call not made lowers the
detection rate and cannot invent anything. `session_bootstrap` is different in
kind: if it is not called, the active knowledge never arrives at all, and the
session has no way to notice, because v2 removed the search it could have gone
looking with. So on a client that has hooks, the opening is delivered by the
one party that cannot forget to.

Compaction is the same failure through a second door. The session id does not
change, `session_bootstrap` is called once by its own contract, and what the
push wrote into the conversation is exactly what a compaction discards.

SessionStart is the event whose plain stdout is documented as reaching the
model as context. The wording matters more than usual: what is printed has to
read as knowledge being handed over, not as a fresh instruction from an
unnamed party, because the model receiving it cannot see where it came from.

Nothing here writes to the store beyond the bootstrap's own event row. A
failure prints nothing and exits 0 — and that silence is what makes the two
paths compose: an agent told nothing falls back on the MCP instructions and
calls `session_bootstrap` itself.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

_VENV = pathlib.Path(__file__).resolve().parent.parent / ".venv"
MASHU = _VENV / "Scripts" / "mashu.exe" if os.name == "nt" else _VENV / "bin" / "mashu"

#: A compaction has a fact about it worth stating: this text was here before
#: and was dropped. Saying so is what stops it reading as a new instruction
#: arriving mid-conversation.
REDELIVERY = (
    "Mashu: 直前の圧縮で、セッション開始時に配信された知識が文脈から落ちた。"
    "以下はその再配信であって、新しい指示ではない。"
)

#: At the start of a session there is nothing to re-deliver, so the line says
#: what this is instead — and says the call is done, because the MCP server's
#: instructions ask for it and an agent that reads both should not pay for the
#: same opening twice.
DELIVERY = (
    "Mashu: このセッションに配信される知識。session_bootstrap は済んでいるので、"
    "改めて呼ばなくてよい。"
)


def preamble(source: str) -> str:
    """The line above the opening, chosen by what put the session here."""
    return REDELIVERY if source == "compact" else DELIVERY


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        event = {}

    # The scope half of the opening is resolved from the working directory,
    # so where this runs decides what comes back. Taking the session's cwd
    # from the payload rather than inheriting whatever the hook was spawned
    # in is the difference between delivering the scope and delivering the
    # always layer alone. The second failure is silent: the output still looks
    # like a bootstrap, with an empty `scoped` section that reads as "this
    # scope holds nothing".
    where = event.get("cwd")
    if not where or not pathlib.Path(where).is_dir():
        where = None

    command = str(MASHU) if MASHU.exists() else "mashu"
    try:
        done = subprocess.run(
            [command, "bootstrap"],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=where,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    if done.returncode != 0 or not done.stdout.strip():
        return 0

    print(preamble(str(event.get("source") or "")))
    print()
    print(done.stdout.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
