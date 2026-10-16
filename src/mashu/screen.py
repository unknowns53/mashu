"""Terminal input, text measurement, and bounded screen layout."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from mashu.errors import MashuError

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


class Quit(Exception):
    """Raised by q on any screen, so every nested screen closes at once."""


# Counted apart from _SESSION_DEPTH, which stays 0 when stdout is not a terminal.
_ENTRY_DEPTH = 0


def fullscreen(function):
    """Run a TUI entry point in the shared, re-entrant terminal session."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        global _ENTRY_DEPTH
        _ENTRY_DEPTH += 1
        try:
            with terminal_session():
                return function(*args, **kwargs)
        except Quit:
            # Only the outermost entry point ends the run; a nested one passes q upward.
            if _ENTRY_DEPTH > 1:
                raise
            return 0
        finally:
            _ENTRY_DEPTH -= 1

    return wrapped


def _enter_alternate_screen() -> None:
    """Enter lazily, so an empty queue can still print a normal one-line result."""
    global _ALTERNATE_ACTIVE

    if _SESSION_DEPTH and not _ALTERNATE_ACTIVE and sys.stdout.isatty():
        sys.stdout.write("\x1b[?1049h")
        sys.stdout.flush()
        _ALTERNATE_ACTIVE = True


def in_alternate_screen() -> bool:
    """Whether the alternate buffer is up, where a printed line is painted over or lost."""
    return _ALTERNATE_ACTIVE


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
        try:
            key = _byte(descriptor)
        except KeyboardInterrupt:
            # cbreak leaves ISIG on, so Ctrl-C arrives as SIGINT rather than as "\x03".
            key = "\x03"
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


def edit_long_text(title: str, current: str) -> str:
    """Edit one body of text, in an outside editor only when the person configured one."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        revised = _run_editor(editor, current)
    else:
        result = edit_text(title, current)
        if isinstance(result, Cancelled):
            raise MashuError("edit cancelled")
        revised = result.text
    revised = revised.strip()
    if not revised:
        raise MashuError("the text cannot be empty")
    return revised


def _run_editor(editor: str, current: str) -> str:
    command = shlex.split(editor)
    if not command:
        raise MashuError("editor command is empty")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        path = Path(handle.name)
        handle.write(current)
    try:
        try:
            subprocess.run([*command, str(path)], check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise MashuError(
                f"editor could not be run with {shlex.join(command)}: {error}"
            ) from error
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


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


def wrap(text: str, indent: str = "  ", width: int | None = None, hang: str | None = None) -> str:
    """Wrap for reading, keeping the line breaks whoever wrote it put in.

    With hang, every row after the very first starts there, so text that follows a
    label or an id stays in its own column.
    """
    width = width or terminal_width()
    rest = indent if hang is None else hang
    out: list[str] = []
    for line in text.strip().splitlines():
        if not line.strip():
            out.append("")
            continue
        bullet = "  " if line.lstrip().startswith(("-", "*", "•")) else ""
        out.extend(_rows(line.strip(), width, rest if out else indent, rest + bullet))
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


def list_screen(head: str, rows: list[str], at: int, keys: str, note: str = "") -> str:
    """A heading, as much of a list as fits under it, and the keys on the last line."""
    room = max(1, _free_lines(head, keys))
    note, used = _note_block(note, 1 + max(0, room - len(rows)))
    listed = _list_block(rows, at, room - (used - 1))
    return "\n".join((head, "", *listed, "", note, keys))


def list_with_preview(
    head: str,
    rows: list[str],
    at: int,
    preview: str,
    keys: str,
    note: str = "",
    *,
    more: str = "⏎ reads all",
) -> str:
    """A heading, a list, the selected row's preview, a note line, and the keys last.

    Where each part sits depends on the terminal, the heading, the keys and the row
    count, never on the preview, so moving the cursor moves nothing but the cursor.
    """
    listed = list_room(head, keys, len(rows))
    room = max(0, _free_lines(head, keys) - listed)
    note, used = _note_block(note, 1 + room)
    room -= used - 1
    shown = _preview_block(preview, room, more) if room >= 2 else [""] * room
    return "\n".join((head, "", *_list_block(rows, at, listed), "", *shown, note, keys))


def list_room(head: str, keys: str, count: int) -> int:
    """The rows a list with a preview under it gets, which is also one page of movement.

    The list keeps every row up to two thirds of the free lines, and never fewer than
    eight, so a 24-line terminal still shows a usable list.
    """
    free = _free_lines(head, keys)
    return max(0, min(count, max(8, free * 2 // 3), max(1, free)))


def _free_lines(head: str, keys: str) -> int:
    """Lines left between a heading and its keys.

    The fixed parts are the blank under the heading, the blank under the list, the
    line kept above the keys for a note, and the cursor's line under the keys, which
    a typed prompt uses and printing onto would scroll the screen.
    """
    height = shutil.get_terminal_size((WIDTH, 24)).lines
    return height - height_of(head) - height_of(keys) - 4


def _list_block(rows: list[str], at: int, room: int) -> list[str]:
    shown, above, below = fit(rows, at, room)
    block = [*([above] if above else []), *shown, *([below] if below else [])]
    return block + [""] * (room - len(block))


def _preview_block(text: str, room: int, more: str) -> list[str]:
    lines = text.splitlines()
    shown = fill(lines, room)
    if len(shown) < len(lines):
        shown = fill(lines, room - 1)
        hint = f"; {more}" if more else ""
        shown.append(dim(f"  … {len(lines) - len(shown)} more line(s){hint}"))
    across = terminal_width()
    used = sum(height_of_line(line, across) for line in shown)
    return shown + [""] * (room - used)


def _note_block(note: str, most: int) -> tuple[str, int]:
    """The note and the lines it takes, cut to one plain line when it needs more than most."""
    if not note:
        return "", 1
    if height_of(note) <= most:
        return note, height_of(note)
    first = re.sub(r"\x1b\[[0-9;]*m", "", note).strip("\n").split("\n")[0]
    return clip(first, terminal_width() - 1), 1


def fit(rows: list[str], at: int, room: int) -> tuple[list[str], str, str]:
    """The part of a list that fits in the room given, kept around the cursor."""
    if len(rows) <= room:
        return rows, "", ""
    if room < 3:
        # No room for the lines that say what is not being shown.
        top = max(0, min(at - room // 2, len(rows) - room))
        return rows[top : top + room], "", ""
    inner = room - 2
    top = max(0, min(at - inner // 2, len(rows) - inner))
    # At either end only one marker shows and the list takes its line, so the list
    # always ends on the same line.
    if top == 0:
        return rows[: room - 1], "", f"  ↓ {len(rows) - room + 1} more"
    if top + inner >= len(rows):
        top = len(rows) - room + 1
        return rows[top:], f"  ↑ {top} more", ""
    return rows[top : top + inner], f"  ↑ {top} more", f"  ↓ {len(rows) - top - inner} more"


def paged(text: str, offset: int, under: str = "") -> tuple[str, int]:
    """As much of one item as fits, and where the next screenful starts.

    Every page is padded to the same height, so what is printed under it stays put.
    """
    lines = text.splitlines()
    head, body = lines[:3], lines[3:]
    space = room_under("\n".join(head), under)
    across = terminal_width()

    shown = fill(body[offset:], space)
    more = 0
    if offset + len(shown) < len(body):
        shown = fill(body[offset:], space - 1)
        left = len(body) - offset - len(shown)
        shown.append(f"  … {left} more line(s), space to go on")
        more = offset + len(shown) - 1
    used = sum(height_of_line(line, across) for line in shown)
    return "\n".join(head + shown + [""] * (space - used)), more


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
