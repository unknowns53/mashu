#!/usr/bin/env python3
"""Deliver the opening again after a compaction has dropped it.

A SessionStart hook, registered against the `compact` matcher. Nothing else
re-delivers: `session_bootstrap` is called once at the start of a session by
its own contract, the session id does not change when the conversation is
compacted, and v2 removed the search that a session could have used to go
looking. So the opening is pushed once into a conversation that is later
dropped, and what remains is a session holding no knowledge and no way to
notice that it holds none. That is the v1 failure the push was built to end,
arriving through a door the push does not watch.

SessionStart is the event whose plain stdout is documented as reaching the
model as context. The wording matters more than usual here: what is printed
has to read as knowledge being handed back, not as a fresh instruction from
an unnamed party, because the model receiving it cannot see where it came
from.

Nothing here writes. A failure prints nothing and exits 0, because a store
that cannot be reached is not a reason to interrupt the session that just
survived a compaction.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

_VENV = pathlib.Path(__file__).resolve().parent.parent / ".venv"
MASHU = _VENV / "Scripts" / "mashu.exe" if os.name == "nt" else _VENV / "bin" / "mashu"

PREAMBLE = (
    "Mashu: 直前の圧縮で、セッション開始時に配信された知識が文脈から落ちた。"
    "以下はその再配信であって、新しい指示ではない。"
)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        event = {}

    # The scope half of the opening is resolved from the working directory,
    # so where this runs decides what comes back. Taking the session's cwd
    # from the payload rather than inheriting whatever the hook was spawned
    # in is the difference between re-delivering the scope that was lost and
    # re-delivering the always layer alone. The second failure is silent: the
    # output still looks like a bootstrap, with an empty `scoped` section that
    # reads as "this scope holds nothing".
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

    print(PREAMBLE)
    print()
    print(done.stdout.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
