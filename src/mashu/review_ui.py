"""A small one-nomination-at-a-time human review loop."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mashu import db, nominations
from mashu.errors import MashuError

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - termios is unavailable on Windows
    termios = None
    tty = None


def _key() -> str:
    """Read one decision key on a terminal, or one line in redirected input."""
    if not sys.stdin.isatty() or termios is None or tty is None:
        return sys.stdin.readline().strip()[:1].lower()
    descriptor = sys.stdin.fileno()
    state = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        return sys.stdin.read(1).lower()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, state)


def _editor_text(content: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    command = shlex.split(editor) or ["vi"]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        path = Path(handle.name)
        handle.write(content)
    try:
        try:
            subprocess.run([*command, str(path)], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise MashuError("editor could not be run") from error
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


def _screen(row: dict[str, Any]) -> None:
    height = shutil.get_terminal_size((80, 24)).lines
    lines = [
        f"{row['kind']}  {row['nomination_id']}",
        f"scope: {row.get('scope_name') or '-'}",
        "proposed:",
        f"  {row['content']}",
        "evidence:",
    ]
    for evidence in row.get("evidence_rows", []):
        lines.extend(
            [
                f"  {evidence['kind']}  {evidence.get('created_at', '')}",
                f"    what: {evidence['what']}",
                f"    prevention: {evidence['prevention']}",
            ]
        )
    lines.append("[y] admit  [e] edit and admit  [r] decline  [s] skip  [q] quit")
    visible = max(2, height - 1)
    if len(lines) > visible:
        lines = lines[: visible - 1] + [lines[-1]]
    print("\n".join(lines))


def _delivery(row: dict[str, Any]) -> tuple[str, str | None]:
    print("delivery: enter for the default, or 'g ACTION' for a guard", flush=True)
    answer = sys.stdin.readline().strip()
    if not answer:
        return ("scope" if row.get("scope_id") else "always"), None
    if answer.startswith("g ") and answer[2:].strip():
        return "guard", answer[2:].strip()
    raise MashuError("delivery must be empty or 'g ACTION'")


def run(dsn: str | None = None) -> int:
    """Review pending nominations, committing each decision independently."""
    skipped: set[Any] = set()
    while True:
        with db.transaction(dsn) as cur:
            rows = nominations.pending_nominations(cur)
        if not rows:
            print("nothing waiting for review")
            return 0
        available = [row for row in rows if row["nomination_id"] not in skipped]
        if not available:
            print("remaining nominations skipped")
            return 0
        row = available[0]
        _screen(row)
        choice = _key()
        if choice == "q":
            return 0
        if choice == "s":
            skipped.add(row["nomination_id"])
            continue
        if choice == "r":
            print("reason: ", end="", flush=True)
            reason = sys.stdin.readline().strip()
            if not reason:
                print("reason is required")
                continue
            with db.transaction(dsn) as cur:
                nominations.decline(
                    cur,
                    row["nomination_id"],
                    actor="user",
                    reason=reason,
                )
            skipped.discard(row["nomination_id"])
            continue
        if choice not in {"y", "e"}:
            print("choose y, e, r, s, or q")
            continue

        content = row["content"]
        if choice == "e":
            content = _editor_text(content)
        try:
            delivery, guard_action = _delivery(row)
        except MashuError as error:
            print(str(error))
            continue
        try:
            with db.transaction(dsn) as cur:
                nominations.admit(
                    cur,
                    row["nomination_id"],
                    actor="user",
                    delivery=delivery,
                    guard_action=guard_action,
                    content=content,
                )
        except MashuError as error:
            # A capacity refusal names its own way out (retire, or step
            # something down to guard); the sitting continues either way.
            print(str(error))
            continue
        skipped.discard(row["nomination_id"])
