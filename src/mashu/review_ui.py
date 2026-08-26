"""The review sitting: the queue on one screen, one candidate on the next.

A decision is asked for one candidate at a time, and that part is not
negotiable — an admission is a permanent seat, and seats are given out singly.
What was wrong with printing them one after another is different: a reader who
cannot see what is waiting cannot choose what to spend ten minutes on, and a
page that scrolls away above the prompt cannot be read back. So the sitting is
two screens and no more. The queue, everything pending on one line each, moved
through with the arrows; and one candidate opened in a keystroke, its whole
body and every pain under it, paged so that nothing is ever printed past the
bottom of the terminal.

There is no key that approves the rest. v1 had one because v1's volume needed
one, and the reason it is not here is the same reason the batch machinery went
out: at a few candidates a week, the thing that a bulk key saves time on is
exactly the thing this queue exists to make somebody do.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from mashu import db, nominations, tokens
from mashu.errors import MashuError

try:
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - these are absent on Windows
    select = None  # type: ignore[assignment]
    termios = None
    tty = None

#: The width text is laid out at when the terminal is wider than reads well.
WIDTH = 88


# --------------------------------------------------------------------------
# reading one key at a time
# --------------------------------------------------------------------------
#: What a terminal sends, and what this calls it. The escape on its own means
#: go back, which is also what the left arrow and backspace mean here.
_TOKENS = {
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\x1b[5~": "pageup",
    "\x1b[6~": "pagedown",
    "\x1b[H": "home",
    "\x1b[F": "end",
    "\r": "enter",
    "\n": "enter",
    "\x7f": "left",
    "\x1b": "left",
    "\x03": "q",
    "\x04": "q",
    " ": "space",
}


def _getkey() -> str:
    """One keystroke, without waiting for a return.

    A review that costs a whole typed line per decision is a review that does
    not happen. Terminals hand arrow keys over as escape sequences, so the
    escape has to be read and then looked at again: on its own it means go
    back, and followed by a bracket it is an arrow.

    Where there is no terminal to put into this mode, a typed line stands in
    for a keystroke and everything above still works — one key more. That path
    is not a courtesy to pipes; it is how the sitting is tested at all.
    """
    if termios is None or tty is None or select is None or not sys.stdin.isatty():
        try:
            typed = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "q"
        return _TOKENS.get(typed, typed[:1].lower() or "enter")

    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    try:
        # TCSANOW, not the default TCSAFLUSH: the default discards whatever
        # was typed before the mode switch, and the switch happens between
        # every two keys. A ⏎ pressed on the heels of an arrow lands in that
        # gap, and a key that is thrown away reads as a key that did nothing.
        tty.setcbreak(descriptor, termios.TCSANOW)
        key = _byte(descriptor)
        # Read the descriptor rather than sys.stdin. A text stream keeps its
        # own buffer, so the bracket and the letter of an arrow sequence can
        # already be inside Python while select still reports the descriptor
        # as empty, and every arrow then arrives as three unrelated keys.
        if key == "\x1b" and select.select([descriptor], [], [], 0.05)[0]:
            key += _byte(descriptor)
            if key.endswith("["):
                key += _csi(descriptor)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
    return _TOKENS.get(key, key.lower())


def _csi(descriptor: int) -> str:
    """The rest of an escape sequence, up to and including the byte that ends it.

    Arrows end after one byte and the page keys do not: Page Down is ESC [ 6 ~,
    so stopping at the first byte leaves a tilde in the buffer and the terminal
    delivers one unknown key followed by another. Both are ignored, and a key
    pressed once with nothing happening twice reads as a key that does nothing.
    """
    out = ""
    while select.select([descriptor], [], [], 0.05)[0]:
        byte = _byte(descriptor)
        out += byte
        if "@" <= byte <= "~":
            break
    return out


def _byte(descriptor: int) -> str:
    """One byte off the terminal, with the end of input read as leaving."""
    try:
        raw = os.read(descriptor, 1)
    except OSError:
        return "\x04"
    return raw.decode("utf-8", "replace") if raw else "\x04"


def _typed(prompt: str) -> str | None:
    """A line, for the reasons a decision is not allowed to go without.

    Nothing typed cancels: an empty reason would be a decision recorded with
    the one part of it that mattered left blank.
    """
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer:
        print("  never mind")
        return None
    return answer


def _screen(text: str) -> None:
    """Repaint. What is being decided about should be the whole view."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[H\x1b[2J")
    print(text)


# --------------------------------------------------------------------------
# measuring, so that nothing is printed past the bottom of the screen
# --------------------------------------------------------------------------
def _width() -> int:
    """How wide the terminal is, not how wide the writing was laid out to be."""
    return max(40, shutil.get_terminal_size((WIDTH, 24)).columns)


def _across() -> int:
    """The width to lay text out at: the terminal's, but never wider than reads well."""
    return min(WIDTH, _width())


def _room(*fixed: str) -> int:
    """How many lines are left for a list or a page once the fixed parts have theirs.

    The fixed parts are handed in and measured rather than counted into a
    number here. A number is right until somebody adds a line to a heading,
    and then it is wrong everywhere the number was used and nothing says so.

    The one line taken off the end is the newline print() adds after a screen.
    """
    used = sum(_rows_of(text) for text in fixed)
    return max(0, shutil.get_terminal_size((WIDTH, 24)).lines - used - 1)


def _rows_of(text: str) -> int:
    """How many terminal lines a printed block takes.

    Split on the newline rather than by splitlines, which drops a trailing
    empty line: "a\\n" prints two lines and splitlines calls it one.
    """
    width = _width()
    return sum(_rows(line, width) for line in text.split("\n"))


def _rows(text: str, width: int) -> int:
    """How many terminal lines one written line takes once it wraps."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return max(1, -(-_cells(plain) // width))


def _cells(text: str) -> int:
    """How wide this is on a terminal, counting the double-width characters as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _clip(text: str, cells: int) -> str:
    """Cut a line to fit, measuring in cells rather than in characters."""
    if _cells(text) <= cells:
        return text
    out, used = [], 0
    for ch in text:
        used += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used > cells - 1:
            break
        out.append(ch)
    return "".join(out) + "…"


def _pad(text: str, cells: int) -> str:
    """Fill a column out to a width, counting cells so a Japanese name lines up too."""
    clipped = _clip(text, cells)
    return clipped + " " * max(0, cells - _cells(clipped))


def _wrap(text: str, indent: str = "  ") -> str:
    """Wrap for reading, keeping the line breaks whoever wrote it put in.

    Filling the whole thing as one paragraph collapses a list of points into a
    wall, and the review is the one place where reading is the work.
    """
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            out.append("")
            continue
        hang = indent + "  " if line.lstrip().startswith(("-", "*", "•")) else indent
        out.append(
            textwrap.fill(
                line.strip(), width=_across(), initial_indent=indent, subsequent_indent=hang
            )
        )
    return "\n".join(out)


def _trailer(*parts: str) -> str:
    """Everything printed under a page or a list, as one measurable block.

    One string, because what follows the screen has to be measured before the
    screen is built and printed after it, and two ways of assembling it is one
    way too many.
    """
    return "\n".join(part for part in parts if part)


def _list_screen(head: str, rows: list[str], at: int, keys: str) -> str:
    """A heading, as much of a list as fits under it, and the keys.

    Composed and measured against the same string, in one place: measuring the
    parts separately and assembling them separately is how a heading that ends
    in a newline comes to cost two lines and be counted as one.
    """
    around = f"{head}\n\n\n{keys}"
    shown, above, below = _fit(rows, at, _room(around))
    body = [line for line in (above, *shown, below) if line]
    return f"{head}\n\n" + "\n".join(body) + f"\n\n{keys}"


def _fit(rows: list[str], at: int, room: int) -> tuple[list[str], str, str]:
    """The part of a list that fits in the room given, kept around the cursor."""
    if len(rows) <= room:
        return rows, "", ""
    room = max(1, room - 2)  # the two lines that say what is not being shown
    top = max(0, min(at - room // 2, len(rows) - room))
    above = f"  ↑ {top} more" if top else ""
    below = f"  ↓ {len(rows) - top - room} more" if top + room < len(rows) else ""
    return rows[top : top + room], above, below


def _paged(text: str, offset: int, trailer: str = "") -> tuple[str, int]:
    """As much of one candidate as fits, and where the next screenful starts.

    The first lines stay on screen whatever the offset: what is being decided
    about should not scroll away from the deciding.

    Filled twice on purpose. The line that says how much is left is itself a
    line, so a page filled to the brim and then told it is not the whole thing
    comes out one row past the bottom of the screen. The first fill answers
    whether that line is needed; the second makes room for it.
    """
    lines = text.splitlines()
    head, body = lines[:3], lines[3:]
    room = _room("\n".join(head), trailer)

    shown = _fill(body[offset:], room)
    if offset + len(shown) >= len(body):
        return "\n".join(head + shown), 0

    shown = _fill(body[offset:], room - 1)
    left = len(body) - offset - len(shown)
    marker = f"  … {left} more line(s), space to go on"
    return "\n".join(head + shown + [marker]), offset + len(shown)


def _fill(lines: list[str], room: int) -> list[str]:
    """As many of these as fit in the rows given, counting the ones that wrap."""
    width = _width()
    out, used = [], 0
    for line in lines:
        cost = _rows(line, width)
        if used + cost > room:
            break
        out.append(line)
        used += cost
    return out


# --------------------------------------------------------------------------
# the screens
# --------------------------------------------------------------------------
_QUEUE_KEYS = "  ↑↓ move   ⏎ open   s put off   ? help   q leave"
_ITEM_KEYS = "  y admit   e edit   r turn down   s put off   ↑↓ next   ← queue   ? help   q leave"

#: What the keys do, for the one keystroke that asks. It lives a keystroke
#: away rather than under every screen: a hint block long enough to explain
#: itself is a hint block competing with the thing being read.
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
        page, more = _paged(f"  what each key does\n\n\n{body}", offset, _HELP_KEYS)
        _screen(page + "\n" + _HELP_KEYS)
        if _getkey() != "space" or not more:
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
    width = _width()
    lines = []
    for number, row in enumerate(rows):
        body = " ".join((row["content"] or "").split())
        mark = "·" if row.get("deferred_at") else " "
        line = _clip(
            f" {mark} {_short(row['nomination_id'])}  {_pad(row['kind'], 12)} "
            f"{_pad(row.get('scope_name') or '-', 12)} {_days(row):>3}d  {body}",
            width - 1,
        )
        lines.append(f"\x1b[1m▸{line[1:]}\x1b[0m" if number == at else line)

    head = f"{len(rows)} waiting for review"
    if hidden:
        head += f"; {hidden} deferred; --all to see them"
    return _list_screen(head, lines, at, keys)


def _item_text(row: dict[str, Any], place: int, total: int) -> str:
    """One candidate as a page of its own, with the pains it rests on under it.

    The evidence is on the same page as the body rather than a keystroke away,
    because the question being asked is not whether the sentence is true; it
    is whether these particular pains are worth a permanent seat.
    """
    label = f" {place} of {total} "
    across = _across()
    cost = tokens.pushed_cost([row["content"]])
    lines = [
        "─" * 4 + label + "─" * max(4, across - 4 - _cells(label)),
        f"{row['kind']}  [{row.get('scope_name') or '-'}]  {_short(row['nomination_id'])}  "
        f"waiting {_days(row)} day(s)  tokens ~{cost}",
        "",
    ]
    if row.get("deferred_at"):
        lines.extend([_wrap(f"(put off earlier: {row.get('defer_reason') or ''})"), ""])
    lines.extend([_wrap(row["content"]), "", "  evidence"])
    for evidence in row.get("evidence_rows", []):
        created = evidence.get("created_at")
        when = created.date().isoformat() if isinstance(created, datetime) else str(created or "")
        lines.append(f"    {evidence['kind']}  {when}")
        lines.append(_wrap(f"what: {evidence['what']}", indent="      "))
        lines.append(_wrap(f"prevention: {evidence['prevention']}", indent="      "))
    lines.append("─" * across)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# carrying one decision into the store
# --------------------------------------------------------------------------
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
    """Where the admitted memory is delivered from, asked once, at admission.

    The default is the narrowest place it can live: its own scope where it has
    one, and everywhere only when it has none.
    """
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
    """Admit one candidate in a transaction of its own.

    One transaction per decision is what lets a reader stop anywhere: what is
    behind them is committed, and a sitting does not have to be finished to
    have been worth starting. That only holds if a refusal stops the decision
    and not the sitting, so what the store declines to do comes back as
    something to say. Returns that, empty when it simply worked.
    """
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
        # A capacity refusal names its own way out (retire something, or step
        # it down to guard); the sitting continues either way, and the reader
        # stays on the candidate rather than finding themselves one further on
        # with nothing recorded behind them.
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


# --------------------------------------------------------------------------
# a sitting
# --------------------------------------------------------------------------
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
            under = _trailer(_ITEM_KEYS, note)
            page, more = _paged(_item_text(rows[at], at + 1, len(rows)), scroll, under)
            _screen(page + "\n" + under)
        else:
            more = 0
            _screen(_queue_screen(rows, at, hidden, _trailer(_QUEUE_KEYS, note)))
        key = _getkey()
        note = ""  # it has been read now; the next screen starts clean

        if key == "?":
            _help()
            continue
        if key == "q":
            return 0

        if reading and key in ("space", "pagedown"):
            # Read on where a candidate is longer than the screen. Space also
            # steps to the next candidate where it is not, because in both
            # cases it means carry on; the page keys stay on the page.
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
                reason = _typed("  why put it off? ")
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
            reason = _typed("  why turn it down? " if key == "r" else "  why put it off? ")
            if reason is None:
                continue
            note = _decide(dsn, rows[at], key, reason)
        else:
            continue

        # A decision the store refused is not a decision: the queue is not
        # re-read and the reader keeps their place, with the refusal under it.
        if note:
            continue
        rows, hidden = _queue(dsn, show_deferred)
        back, scroll = [], 0
