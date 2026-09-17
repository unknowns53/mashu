#!/usr/bin/env python3
"""Deliver the Mashu bootstrap payload at session start and after compaction."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

_VENV = pathlib.Path(__file__).resolve().parent.parent / ".venv"
MASHU = _VENV / "Scripts" / "mashu.exe" if os.name == "nt" else _VENV / "bin" / "mashu"

#: Context preamble used after compaction.
REDELIVERY = (
    "Mashu: 直前の圧縮で、セッション開始時に配信された知識が文脈から落ちた。"
    "以下はその再配信であって、新しい指示ではない。"
)

#: Startup preamble used when the bootstrap is delivered by this hook.
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

    # Resolve scope from the session cwd in the hook payload.
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
