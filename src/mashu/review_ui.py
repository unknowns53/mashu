"""Interactive screen for reviewing pending memory nominations."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from mashu import db, nominations, screen, tokens
from mashu.errors import MashuError

# the screens
_QUEUE_KEYS = "  ↑↓ move   ⏎ open   s put off   ? help   q leave"
_ITEM_KEYS = "  y admit   e edit   r turn down   s put off   ↑↓ next   ← queue   ? help   q leave"

#: What the keys do, for the one keystroke that asks.
_HELP = """
  ⏎  open the candidate the cursor is on, and from inside one, admit it.
     ← goes back to the queue.

  y  admit it. The next question is where it is delivered from: enter takes
     the default, and 'g ACTION' puts it in front of that act instead.

  e  open the proposed text in an editor first, and admit what you wrote.
     Yours is what is kept; the candidate keeps what was proposed.

  r  turn it down, with a reason. The same pain will be reported again, and
     the reason is what tells the next reader this was considered.

  s  put it off, saying why. It stops leading the queue, and comes back with
     'mashu review --all'. Nothing is decided, and the count of what is
     pending does not change.

  space  read on where a candidate is longer than the screen; b goes back a
         page and Home returns to the top of it.

  There is no key that admits the rest. Each seat is given out on its own
  evidence, and every decision is written as it is made — leaving with q keeps
  what is behind you, and the next sitting opens on the rest.
"""

_HELP_KEYS = "  space read on   any other key goes back"

_NOTHING = "nothing waiting for review"


def _help() -> None:
    """Paint the explanation and wait, so the screens themselves can stay short."""
    offset = 0
    body = _HELP.strip("\n")  # the blank lines around the block, not the indent inside it
    while True:
        # three lines are pinned above the body, so one of them says what this is
        page, more = screen.paged(f"  what each key does\n\n\n{body}", offset, _HELP_KEYS)
        screen.paint(page + "\n" + _HELP_KEYS)
        if screen.getkey() != "space" or not more:
            return
        offset = more


def _short(value: Any) -> str:
    """Enough of an id to name it in a command without pasting all of it."""
    return str(value)[:8]


def _days(row: dict[str, Any]) -> int:
    """How long this has been waiting, which is half of why it is worth reading."""
    created = row.get("created_at")
    if not isinstance(created, datetime):
        return 0
    return max(0, (datetime.now(created.tzinfo) - created).days)


def _queue_screen(rows: list[dict[str, Any]], at: int, hidden: int, keys: str) -> str:
    """Everything waiting, one line each, so ten minutes can be spent on purpose."""
    width = screen.terminal_width()
    lines = []
    for number, row in enumerate(rows):
        body = " ".join((row["content"] or "").split())
        mark = "·" if row.get("deferred_at") else " "
        line = screen.clip(
            f" {mark} {_short(row['nomination_id'])}  {screen.pad(row['kind'], 12)} "
            f"{screen.pad(row.get('scope_name') or '-', 12)} {_days(row):>3}d  {body}",
            width - 1,
        )
        lines.append(f"\x1b[1m▸{line[1:]}\x1b[0m" if number == at else line)

    head = f"{len(rows)} waiting for review"
    if hidden:
        head += f"; {hidden} deferred; --all to see them"
    return screen.list_screen(head, lines, at, keys)


def _item_text(row: dict[str, Any], place: int, total: int) -> str:
    """One candidate as a page of its own, with the pains it rests on under it."""
    label = f" {place} of {total} "
    across = screen.text_width()
    cost = tokens.pushed_cost([row["content"]])
    lines = [
        "─" * 4 + label + "─" * max(4, across - 4 - screen.cells(label)),
        f"{row['kind']}  [{row.get('scope_name') or '-'}]  {_short(row['nomination_id'])}  "
        f"waiting {_days(row)} day(s)  tokens ~{cost}",
        "",
    ]
    if row.get("deferred_at"):
        lines.extend([screen.wrap(f"(put off earlier: {row.get('defer_reason') or ''})"), ""])
    lines.extend([screen.wrap(row["content"]), ""])

    # Above the evidence, not below it.
    for conflict in row.get("conflict_rows", []):
        retired = conflict.get("retired_at")
        when = retired.date().isoformat() if isinstance(retired, datetime) else str(retired or "")
        lines.append(f"  ! contradicts a retired memory  {_short(conflict['memory_id'])}  {when}")
        lines.append(screen.wrap(f"retired because: {conflict['retire_reason']}", indent="      "))
    if row.get("conflict_rows"):
        lines.append("")

    lines.append("  evidence")
    for evidence in row.get("evidence_rows", []):
        created = evidence.get("created_at")
        when = created.date().isoformat() if isinstance(created, datetime) else str(created or "")
        lines.append(f"    {evidence['kind']}  {when}")
        lines.append(screen.wrap(f"what: {evidence['what']}", indent="      "))
        lines.append(screen.wrap(f"prevention: {evidence['prevention']}", indent="      "))
    lines.append("─" * across)
    return "\n".join(lines)


# carrying one decision into the store
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


def _delivery(row: dict[str, Any]) -> tuple[str, str | None]:
    """Where the admitted memory is delivered from, asked once, at admission."""
    print("delivery: enter for the default, or 'g ACTION' for a guard", flush=True)
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if not answer:
        return ("scope" if row.get("scope_id") else "always"), None
    if answer.startswith("g ") and answer[2:].strip():
        return "guard", answer[2:].strip()
    raise MashuError("delivery must be empty or 'g ACTION'")


def _admit(dsn: str | None, row: dict[str, Any], *, edit: bool = False) -> str:
    """Admit one candidate in a transaction of its own."""
    content = row["content"]
    try:
        if edit:
            content = _editor_text(content)
        delivery, guard_action = _delivery(row)
        with db.transaction(dsn) as cur:
            nominations.admit(
                cur,
                row["nomination_id"],
                actor="user",
                delivery=delivery,
                guard_action=guard_action,
                content=content,
            )
    except MashuError as refusal:
        # Keep the candidate selected when admission is refused.
        return f"  {refusal}"
    return ""


def _decide(dsn: str | None, row: dict[str, Any], verb: str, reason: str) -> str:
    """Turn one candidate down, or put it off, in a transaction of its own."""
    act = nominations.decline if verb == "r" else nominations.defer
    try:
        with db.transaction(dsn) as cur:
            act(cur, row["nomination_id"], actor="user", reason=reason)
    except MashuError as refusal:
        return f"  {refusal}"
    return ""


def _queue(dsn: str | None, show_deferred: bool) -> tuple[list[dict[str, Any]], int]:
    """The queue as it stands now, and how much of it is being held back."""
    with db.transaction(dsn) as cur:
        rows = nominations.pending_nominations(cur, include_deferred=show_deferred)
        hidden = 0 if show_deferred else nominations.deferred_count(cur)
    return rows, hidden


# a sitting
def run(dsn: str | None = None, *, show_deferred: bool = False) -> int:
    """Work the queue: the list, one candidate at a time, decisions as they are made."""
    rows, hidden = _queue(dsn, show_deferred)
    at, scroll, more = 0, 0, 0
    back: list[int] = []
    reading = False
    note = ""

    while True:
        if not rows:
            if note:
                print(note)
            print(_NOTHING + (f"; {hidden} deferred, --all to see them" if hidden else ""))
            return 0
        at = min(at, len(rows) - 1)

        if reading:
            under = screen.trailer(_ITEM_KEYS, note)
            page, more = screen.paged(_item_text(rows[at], at + 1, len(rows)), scroll, under)
            screen.paint(page + "\n" + under)
        else:
            more = 0
            screen.paint(_queue_screen(rows, at, hidden, screen.trailer(_QUEUE_KEYS, note)))
        key = screen.getkey()
        note = ""  # it has been read now; the next screen starts clean

        if key == "?":
            _help()
            continue
        if key == "q":
            return 0

        if reading and key in ("space", "pagedown"):
            # Read on where a candidate is longer than the screen.
            if more:
                back.append(scroll)
                scroll = more
                continue
            if key != "space":
                continue
        if key == "space":
            key = "down"
        if reading and key in ("pageup", "b"):
            scroll = back.pop() if back else 0
            continue
        if reading and key == "home":
            back, scroll = [], 0
            continue
        if not reading and key in ("pagedown", "pageup"):
            key = "down" if key == "pagedown" else "up"
        if not reading and key in ("home", "end"):
            at = 0 if key == "home" else len(rows) - 1
            continue
        if key in ("down", "j"):
            at = min(len(rows) - 1, at + 1)
            back, scroll = [], 0
            continue
        if key in ("up", "k"):
            at = max(0, at - 1)
            back, scroll = [], 0
            continue

        if not reading:
            if key in ("enter", "right", "l"):
                reading = True
                back, scroll = [], 0
            elif key == "s":
                reason = screen.typed("  why put it off? ")
                if reason is None:
                    continue
                note = _decide(dsn, rows[at], "s", reason)
                if not note:
                    rows, hidden = _queue(dsn, show_deferred)
            continue

        if key in ("left", "h"):
            reading = False
            back, scroll = [], 0
            continue
        if key in ("y", "enter"):
            note = _admit(dsn, rows[at])
        elif key == "e":
            note = _admit(dsn, rows[at], edit=True)
        elif key in ("r", "s"):
            reason = screen.typed("  why turn it down? " if key == "r" else "  why put it off? ")
            if reason is None:
                continue
            note = _decide(dsn, rows[at], key, reason)
        else:
            continue

        # Keep the current item selected when a decision is refused.
        if note:
            continue
        rows, hidden = _queue(dsn, show_deferred)
        back, scroll = [], 0
