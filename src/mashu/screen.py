"""A terminal one screenful at a time: keys in, a measured screen out.

Extracted from the review sitting when the task-closing picker needed the same
things. Nothing here knows what is being decided; it knows how wide a terminal
is, how many rows a block of text will take once it wraps, and what byte a
terminal sent. Both callers repaint the whole screen for each key, so what
this has to get right is that a screen is never built taller than the terminal
it is printed on: a line past the bottom scrolls the heading away, and a
heading that has scrolled away is a reader who no longer knows what the list
is of.

Widths are measured in cells rather than characters throughout. The names and
the reasons in this store are frequently Japanese, and a column laid out by
character count puts every such row out of alignment with the one above it.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import unicodedata

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


def getkey() -> str:
    """One keystroke, without waiting for a return.

    A decision that costs a whole typed line is a decision that does not
    happen. Terminals hand arrow keys over as escape sequences, so the escape
    has to be read and then looked at again: on its own it means go back, and
    followed by a bracket it is an arrow.

    Where there is no terminal to put into this mode, a typed line stands in
    for a keystroke and everything above still works — one key more. That path
    is not a courtesy to pipes; it is how these screens are tested at all.
    """
    if termios is None or tty is None or select is None or not sys.stdin.isatty():
        try:
            typed_line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "q"
        return _TOKENS.get(typed_line, typed_line[:1].lower() or "enter")

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


def typed(prompt: str) -> str | None:
    """A line, for the parts of a decision that cannot be a keystroke.

    Nothing typed cancels. A caller that is willing to take an empty answer as
    an answer asks for the line itself; this is for the ones where a blank
    would be the one part that mattered left out.
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


def paint(text: str) -> None:
    """Repaint. What is being decided about should be the whole view."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[H\x1b[2J")
    print(text)


# --------------------------------------------------------------------------
# measuring, so that nothing is printed past the bottom of the screen
# --------------------------------------------------------------------------
def terminal_width() -> int:
    """How wide the terminal is, not how wide the writing was laid out to be."""
    return max(40, shutil.get_terminal_size((WIDTH, 24)).columns)


def text_width() -> int:
    """The width to lay text out at: the terminal's, but never wider than reads well."""
    return min(WIDTH, terminal_width())


def room_under(*fixed: str) -> int:
    """How many lines are left for a list or a page once the fixed parts have theirs.

    The fixed parts are handed in and measured rather than counted into a
    number here. A number is right until somebody adds a line to a heading,
    and then it is wrong everywhere the number was used and nothing says so.

    The one line taken off the end is the newline print() adds after a screen.
    """
    used = sum(height_of(text) for text in fixed)
    return max(0, shutil.get_terminal_size((WIDTH, 24)).lines - used - 1)


def height_of(text: str) -> int:
    """How many terminal lines a printed block takes.

    Split on the newline rather than by splitlines, which drops a trailing
    empty line: "a\\n" prints two lines and splitlines calls it one.
    """
    across = terminal_width()
    return sum(height_of_line(line, across) for line in text.split("\n"))


def height_of_line(text: str, width: int) -> int:
    """How many terminal lines one written line takes once it wraps."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return max(1, -(-cells(plain) // width))


def cells(text: str) -> int:
    """How wide this is on a terminal, counting the double-width characters as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def clip(text: str, limit: int) -> str:
    """Cut a line to fit, measuring in cells rather than in characters."""
    if cells(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        used += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used > limit - 1:
            break
        out.append(ch)
    return "".join(out) + "…"


def pad(text: str, limit: int) -> str:
    """Fill a column out to a width, counting cells so a Japanese name lines up too."""
    clipped = clip(text, limit)
    return clipped + " " * max(0, limit - cells(clipped))


def wrap(text: str, indent: str = "  ") -> str:
    """Wrap for reading, keeping the line breaks whoever wrote it put in.

    Filling the whole thing as one paragraph collapses a list of points into a
    wall, and these screens are where reading is the work.
    """
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            out.append("")
            continue
        hang = indent + "  " if line.lstrip().startswith(("-", "*", "•")) else indent
        out.append(
            textwrap.fill(
                line.strip(), width=text_width(), initial_indent=indent, subsequent_indent=hang
            )
        )
    return "\n".join(out)


def trailer(*parts: str) -> str:
    """Everything printed under a page or a list, as one measurable block.

    One string, because what follows the screen has to be measured before the
    screen is built and printed after it, and two ways of assembling it is one
    way too many.
    """
    return "\n".join(part for part in parts if part)


def list_screen(head: str, rows: list[str], at: int, keys: str) -> str:
    """A heading, as much of a list as fits under it, and the keys.

    Composed and measured against the same string, in one place: measuring the
    parts separately and assembling them separately is how a heading that ends
    in a newline comes to cost two lines and be counted as one.
    """
    around = f"{head}\n\n\n{keys}"
    shown, above, below = fit(rows, at, room_under(around))
    body = [line for line in (above, *shown, below) if line]
    return f"{head}\n\n" + "\n".join(body) + f"\n\n{keys}"


def fit(rows: list[str], at: int, room: int) -> tuple[list[str], str, str]:
    """The part of a list that fits in the room given, kept around the cursor."""
    if len(rows) <= room:
        return rows, "", ""
    room = max(1, room - 2)  # the two lines that say what is not being shown
    top = max(0, min(at - room // 2, len(rows) - room))
    above = f"  ↑ {top} more" if top else ""
    below = f"  ↓ {len(rows) - top - room} more" if top + room < len(rows) else ""
    return rows[top : top + room], above, below


def paged(text: str, offset: int, under: str = "") -> tuple[str, int]:
    """As much of one item as fits, and where the next screenful starts.

    The first lines stay on screen whatever the offset: what is being decided
    about should not scroll away from the deciding.

    Filled twice on purpose. The line that says how much is left is itself a
    line, so a page filled to the brim and then told it is not the whole thing
    comes out one row past the bottom of the screen. The first fill answers
    whether that line is needed; the second makes room for it.
    """
    lines = text.splitlines()
    head, body = lines[:3], lines[3:]
    space = room_under("\n".join(head), under)

    shown = fill(body[offset:], space)
    if offset + len(shown) >= len(body):
        return "\n".join(head + shown), 0

    shown = fill(body[offset:], space - 1)
    left = len(body) - offset - len(shown)
    marker = f"  … {left} more line(s), space to go on"
    return "\n".join(head + shown + [marker]), offset + len(shown)


def fill(lines: list[str], room: int) -> list[str]:
    """As many of these as fit in the rows given, counting the ones that wrap."""
    across = terminal_width()
    out, used = [], 0
    for line in lines:
        cost = height_of_line(line, across)
        if used + cost > room:
            break
        out.append(line)
        used += cost
    return out
