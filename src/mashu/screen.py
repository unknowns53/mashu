"""Terminal input, text measurement, and bounded screen layout."""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

try:
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - these are absent on Windows
    select = None  # type: ignore[assignment]
    termios = None
    tty = None

#: The width assumed when there is no terminal to measure.
WIDTH = 88


@dataclass(frozen=True)
class Submitted:
    text: str


@dataclass(frozen=True)
class Cancelled:
    pass


InputResult = Submitted | Cancelled

# TUI entry points can be nested (the dashboard opens review and close screens).
# The alternate buffer therefore belongs to the outermost active session only.
_SESSION_DEPTH = 0
_ALTERNATE_ACTIVE = False


@contextmanager
def terminal_session():
    """Keep repaints out of scrollback, restoring the caller's screen on exit."""
    global _ALTERNATE_ACTIVE, _SESSION_DEPTH

    interactive = sys.stdout.isatty()
    if interactive:
        _SESSION_DEPTH += 1
    try:
        yield
    finally:
        if interactive:
            _SESSION_DEPTH -= 1
            if _SESSION_DEPTH == 0 and _ALTERNATE_ACTIVE:
                sys.stdout.write("\x1b[?1049l")
                sys.stdout.flush()
                _ALTERNATE_ACTIVE = False


def fullscreen(function):
    """Run a TUI entry point in the shared, re-entrant terminal session."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with terminal_session():
            return function(*args, **kwargs)

    return wrapped


def _enter_alternate_screen() -> None:
    """Enter lazily, so an empty queue can still print a normal one-line result."""
    global _ALTERNATE_ACTIVE

    if _SESSION_DEPTH and not _ALTERNATE_ACTIVE and sys.stdout.isatty():
        sys.stdout.write("\x1b[?1049h")
        sys.stdout.flush()
        _ALTERNATE_ACTIVE = True


def color_enabled() -> bool:
    """Whether terminal decoration is useful and has not been disabled."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _style(text: str, code: str) -> str:
    """Decorate text for a person's terminal, while keeping redirected output plain."""
    if not color_enabled():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def bold(text: str) -> str:
    return _style(text, "1")


def dim(text: str) -> str:
    return _style(text, "2")


def accent(text: str) -> str:
    return _style(text, "36")


def success(text: str) -> str:
    return _style(text, "32")


def warning(text: str) -> str:
    return _style(text, "33")


def danger(text: str) -> str:
    return _style(text, "31")


def selected(text: str) -> str:
    """Make the current choice distinct without relying on colour alone."""
    marked = f"▸{text[1:]}" if text.startswith(" ") else f"▸ {text}"
    return _style(marked, "1;7")


# Key names mapped from terminal escape sequences.
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
    # The names, for the path where a typed line stands in for a keystroke.
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "enter": "enter",
    "space": "space",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
}


def getkey() -> str:
    """One keystroke, without waiting for a return."""
    if termios is None or tty is None or select is None or not sys.stdin.isatty():
        try:
            typed_line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "q"
        return _TOKENS.get(typed_line, typed_line[:1].lower() or "enter")

    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    try:
        # TCSAFLUSH can discard input buffered between key reads.
        tty.setcbreak(descriptor, termios.TCSANOW)
        key = _byte(descriptor)
        # Read the descriptor rather than sys.stdin.
        if key == "\x1b" and select.select([descriptor], [], [], 0.05)[0]:
            key += _byte(descriptor)
            if key.endswith("["):
                key += _csi(descriptor)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
    return _TOKENS.get(key, key.lower())


def _csi(descriptor: int) -> str:
    """The rest of an escape sequence, up to and including the byte that ends it."""
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


def typed(prompt: str) -> InputResult:
    """A line, for the parts of a decision that cannot be a keystroke."""
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return Cancelled()
    if not answer:
        print("  never mind")
    return Submitted(answer)


def editline(prompt: str, current: str) -> InputResult:
    """Read a line with the current value already in an interactive input buffer."""
    readline_module = None
    if sys.stdin.isatty():
        try:
            import readline as readline_module

            def prefill() -> None:
                readline_module.insert_text(current)

            readline_module.set_startup_hook(prefill)
        except (ImportError, AttributeError):  # pragma: no cover - platform dependent
            readline_module = None
    try:
        return Submitted(input(prompt).strip())
    except (EOFError, KeyboardInterrupt):
        print()
        return Cancelled()
    finally:
        if readline_module is not None:
            readline_module.set_startup_hook()


def edit_text(title: str, current: str) -> InputResult:
    """Open a focused, single-copy editor for one existing body of text."""
    paint(
        "\n".join(
            (
                bold(title),
                dim("Edit the existing text below. Enter saves; Ctrl+C cancels."),
                "",
            )
        )
    )
    return editline("  > ", current)


def paint(text: str) -> None:
    """Repaint. What is being decided about should be the whole view."""
    if sys.stdout.isatty():
        _enter_alternate_screen()
        sys.stdout.write("\x1b[H\x1b[2J")
    print(text)


# measuring, so that nothing is printed past the bottom of the screen
def terminal_width() -> int:
    """How wide the terminal is, which is also the width text is laid out at."""
    return max(40, shutil.get_terminal_size((WIDTH, 24)).columns)


def room_under(*fixed: str) -> int:
    """How many lines are left for a list or a page once the fixed parts have theirs."""
    used = sum(height_of(text) for text in fixed)
    return max(0, shutil.get_terminal_size((WIDTH, 24)).lines - used - 1)


def height_of(text: str) -> int:
    """How many terminal lines a printed block takes."""
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


def wrap(text: str, indent: str = "  ", width: int | None = None) -> str:
    """Wrap for reading, keeping the line breaks whoever wrote it put in."""
    width = width or terminal_width()
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            out.append("")
            continue
        hang = indent + "  " if line.lstrip().startswith(("-", "*", "•")) else indent
        out.extend(_rows(line.strip(), width, indent, hang))
    return "\n".join(out)


# The line-start and line-end prohibitions of JIS X 4051 (kinsoku), cut down to the
# characters agents actually write.
_NO_LINE_START = frozenset(
    "、。，．,.)）」』】〕〉》］｝・ー！？!?：；:;ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々"
)
_NO_LINE_END = frozenset("(（「『【〔〈《［｛")


def _wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in "WF"


def _breakable(line: str, at: int) -> bool:
    """Whether a row may end just before line[at].

    Japanese breaks between any two characters, Latin only at a space, so a word like
    verify_board is never split while a sentence around it still fills the row.
    """
    before, after = line[at - 1], line[at]
    if after == " ":
        return False
    if before == " ":
        return True
    if after in _NO_LINE_START or before in _NO_LINE_END:
        return False
    return _wide(before) or _wide(after)


def _rows(line: str, width: int, indent: str, hang: str) -> list[str]:
    rows, start, lead = [], 0, indent
    while cells(lead) + cells(line[start:]) > width:
        room = max(1, width - cells(lead))
        used, at, cut = 0, start, None
        while at < len(line):
            used += cells(line[at])
            if used > room:
                break
            at += 1
            if at < len(line) and _breakable(line, at):
                cut = at
        if line[at] == " ":
            cut = at + 1
        elif cut is None:
            # A single word wider than the row has nowhere else to go.
            cut = max(at, start + 1)
        end = cut - 1 if line[cut - 1] == " " else cut
        rows.append(lead + line[start:end])
        start, lead = cut, hang
    rows.append(lead + line[start:])
    return rows


def trailer(*parts: str) -> str:
    """Everything printed under a page or a list, as one measurable block."""
    return "\n".join(part for part in parts if part)


def list_screen(head: str, rows: list[str], at: int, keys: str) -> str:
    """A heading, as much of a list as fits under it, and the keys."""
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
    """As much of one item as fits, and where the next screenful starts."""
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
