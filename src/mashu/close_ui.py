"""Interactive screen for reviewing and closing open tasks."""

from __future__ import annotations

import datetime as dt
import shutil
from typing import Any
from uuid import UUID

from mashu import db, screen, tasks
from mashu.errors import MashuError

#: Only a person reaches this screen; the CLI is the only way in.
ACTOR = "user"

#: The three outcomes, on the letters they start with.
_OUTCOME_KEYS = {"c": "completed", "a": "abandoned", "s": "superseded"}

#: The lease, in the four cells the column has.
_ACTIVITY = {"active": "act", "dormant": "dorm"}

_KEYS = (
    "  ↑↓/jk move   Home/End   PgUp/PgDn   / search   ⏎ take proposal\n"
    "  c completed   a abandoned   s superseded   w drop   t renew   ? help   q leave"
)

_HELP = """
  ↑/↓ or j/k move one task. Home and End jump to the first and last task;
     Page Up and Page Down move by one visible screenful.

  /  search task id, name, project, activity, current state, or proposal grounds.
     Search is case-insensitive. Submit an empty search to show everything again.
     ← or Esc clears an active search; with no search it leaves this screen.

  ⏎  close the task on the proposal printed under the list: its outcome and
     its grounds, recorded as your decision. Only where one stands, and where
     the state has moved since it was written, you are asked once more first.

  c  close as completed. a and s close as abandoned and superseded. Each asks
     for a reason and takes an empty one — except where the proposal already
     names that same outcome, when an empty line keeps its grounds.

  w  drop the proposal and say the work is still live. The task stays open
     with its lease renewed, and the agent that proposed it will see that it
     no longer stands.

  t  renew the lease without deciding anything. A dormant task comes back to
     the opening; nothing is said about whether it is finished.

  There is no key that closes the rest. The transcription was the expensive
  part of closing eighteen tasks and it is gone; the judgement was never the
  part worth saving, and it is still asked for one task at a time.
"""

_HELP_KEYS = "  space read on   any other key goes back"

_NOTHING = "no open tasks"


def _short(value: Any) -> str:
    """Enough of an id to name it in a command without pasting all of it."""
    return str(value)[:8]


def _date(value: Any) -> str:
    return value.date().isoformat() if isinstance(value, dt.datetime) else str(value)


def _order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Proposals first, standing ones over overtaken ones."""

    def rank(row: dict[str, Any]) -> int:
        proposal = row.get("proposal")
        if not proposal:
            return 2
        return 1 if proposal["stale"] else 0

    return sorted(rows, key=rank)


def _mark(row: dict[str, Any]) -> str:
    """One character saying whether somebody has already called this finished."""
    proposal = row.get("proposal")
    if not proposal:
        return " "
    return screen.warning("◌") if proposal["stale"] else screen.accent("●")


def _line(row: dict[str, Any], width: int) -> str:
    """One task, as much of it as the terminal is wide."""
    task = row["task"]
    plain_fixed = (
        f"   {_short(task['task_id'])}  {screen.pad(task['project_name'], 10)} "
        f"{screen.pad(_ACTIVITY[row['activity']], 4)}"
    )
    styled_activity = (
        screen.success(screen.pad(_ACTIVITY[row["activity"]], 4))
        if row["activity"] == "active"
        else screen.dim(screen.pad(_ACTIVITY[row["activity"]], 4))
    )
    fixed = (
        f" {_mark(row)} {_short(task['task_id'])}  {screen.pad(task['project_name'], 10)} "
        f"{styled_activity}"
    )
    suffix = f" {row['age_days']:>3}d  {task['name']}"
    room = max(1, width - screen.cells(plain_fixed))
    return f"{fixed}{screen.clip(suffix, room)}"


def _field(label: str, value: Any, *, value_style=None) -> str:
    """One compact, readable state field which cannot wrap past the screen."""
    if isinstance(value, (list, tuple)):
        text = "  •  ".join(str(item) for item in value)
    else:
        text = " ".join(str(value).splitlines())
    prefix = f"  {label:<13}"
    available = max(1, screen.text_width() - screen.cells(prefix))
    shown = screen.clip(text, available)
    if value_style is not None:
        shown = value_style(shown)
    return f"  {screen.bold(f'{label:<13}')}{shown}"


def _detail(row: dict[str, Any], limit: int | None = None) -> str:
    """The selected task's decision context, kept inside the terminal height."""
    task = row["task"]
    title_prefix = f"  ── {_short(task['task_id'])}  "
    title = screen.clip(task["name"], max(1, screen.text_width() - screen.cells(title_prefix)))
    activity = (
        screen.success(row["activity"])
        if row["activity"] == "active"
        else screen.warning(row["activity"])
    )
    lines = [
        screen.bold(f"{title_prefix}{title}"),
        f"  {screen.dim(row['heading'])}  ·  {activity}",
    ]

    proposal = row.get("proposal")
    if proposal:
        said = (
            f"PROPOSAL  proposed {proposal['outcome']} by "
            f"{proposal['proposed_by']} on {proposal['on_date']}"
        )
        lines.append(screen.accent(f"  {said}"))
        lines.append(_field("reason", proposal["reason"], value_style=screen.accent))
        if proposal["stale"]:
            lines.append(
                screen.warning(
                    f"  STALE        state was written since the proposal, as of {row['as_of']}"
                )
            )

    state = row["state"]
    state_fields = (
        ("goal", state.get("goal")),
        ("status", state.get("status_text")),
        ("approach", state.get("approach")),
        ("open questions", state.get("open_questions")),
        ("blockers", state.get("blockers")),
        ("next actions", state.get("next_actions")),
    )
    for label, value in state_fields:
        if value:
            lines.append(_field(label, value))
    if len(lines) == 2 and not proposal:
        lines.append(screen.dim("  (no state written)"))

    if limit is not None and len(lines) > limit:
        hidden = len(lines) - max(1, limit - 1)
        lines = lines[: max(1, limit - 1)]
        lines.append(screen.dim(f"  … {hidden} more detail line(s)"))
    return "\n".join(lines)


def _heading(rows: list[dict[str, Any]], total: int, query: str) -> str:
    standing = sum(1 for row in rows if row.get("proposal"))
    if query:
        head = f"  {len(rows)}/{total} open task(s)  ·  search {screen.accent(repr(query))}"
    else:
        head = f"  {total} open task(s)"
    return head + (f", {standing} proposed closed" if standing else "")


def _screen_text(
    rows: list[dict[str, Any]], at: int, under: str, *, total: int | None = None, query: str = ""
) -> str:
    """The whole screen: how many are open, the list, the cursor's task, the keys."""
    width = screen.terminal_width()
    head = _heading(rows, len(rows) if total is None else total, query)
    lines = []
    for number, row in enumerate(rows):
        line = _line(row, width - 1)
        lines.append(screen.selected(line) if number == at else line)
    if not rows:
        lines.append(screen.dim("  no tasks match; / searches again, ← clears the search"))
    return screen.list_screen(head, lines, at, under)


def _matches(row: dict[str, Any], query: str) -> bool:
    """Whether the task contains the query in one of the user-facing search fields."""
    proposal = row.get("proposal") or {}
    task = row["task"]
    state = row["state"]
    values = (
        task.get("task_id"),
        task.get("name"),
        task.get("project_name"),
        row.get("activity"),
        state.get("goal"),
        state.get("status_text"),
        state.get("approach"),
        *(state.get("open_questions") or []),
        *(state.get("blockers") or []),
        *(state.get("next_actions") or []),
        proposal.get("reason"),
    )
    needle = query.casefold()
    return any(needle in str(value).casefold() for value in values if value is not None)


def _filtered(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    return [row for row in rows if not query or _matches(row, query)]


def _search(current: str) -> str | None:
    """Read a substring; an empty answer intentionally clears the current search."""
    prompt = f"  search [{current}; empty clears]: " if current else "  search [empty clears]: "
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _task_id(row: dict[str, Any]) -> UUID:
    return row["task"]["task_id"]


def _find(rows: list[dict[str, Any]], task_id: UUID | None) -> int | None:
    if task_id is None:
        return None
    return next((number for number, row in enumerate(rows) if _task_id(row) == task_id), None)


def _detail_limit(head: str, note: str) -> int:
    """Leave room for a list row, blank separators, keys, and optional feedback."""
    terminal_lines = shutil.get_terminal_size((screen.WIDTH, 24)).lines
    fixed = screen.height_of(head) + screen.height_of(_KEYS) + screen.height_of(note) + 6
    return max(2, min(12, terminal_lines - fixed))


def _page_size(head: str, under: str) -> int:
    """The number of one-line tasks that fit on the current list page."""
    return max(1, screen.room_under(f"{head}\n\n\n{under}"))


def _help() -> None:
    """Paint the explanation and wait, so the screen itself can stay short."""
    offset = 0
    body = _HELP.strip("\n")
    while True:
        page, more = screen.paged(f"  what each key does\n\n\n{body}", offset, _HELP_KEYS)
        screen.paint(page + "\n" + _HELP_KEYS)
        if screen.getkey() != "space" or not more:
            return
        offset = more


def _reason(row: dict[str, Any], outcome: str) -> str | None:
    """The grounds for a close, where an empty line is an answer."""
    proposal = row.get("proposal")
    if proposal and proposal["outcome"] == outcome:
        prompt = f"  reason [⏎ keeps: {screen.clip(proposal['reason'], 48)}]: "
    else:
        prompt = "  reason [⏎ for none]: "
    try:
        return input(prompt).strip() or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _confirm(question: str) -> bool:
    try:
        return input(f"  {question} [y/N]: ").strip().lower().startswith("y")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _close(dsn: str | None, row: dict[str, Any], outcome: str, reason: str | None) -> str:
    task_id = row["task"]["task_id"]
    with db.transaction(dsn) as cur:
        tasks.close(cur, task_id, outcome=outcome, actor=ACTOR, reason=reason)
    return screen.success(f"  ✓ closed  {_short(task_id)}  {outcome}")


def _open_tasks(dsn: str | None, project: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return _order(tasks.task_list(cur, project=project, activity="open"))


@screen.fullscreen
def run(dsn: str | None = None, *, project: str | None = None) -> int:
    """Work the list: the open tasks, and one decision at a time against them."""
    at, note, query = 0, "", ""
    selected_id: UUID | None = None
    while True:
        try:
            all_rows = _open_tasks(dsn, project)
        except MashuError as error:
            print(error)
            return 1
        if not all_rows:
            if note:
                print(note)
            print(_NOTHING)
            return 0
        rows = _filtered(all_rows, query)
        preserved = _find(rows, selected_id)
        if preserved is not None:
            at = preserved
        elif rows:
            at = max(0, min(at, len(rows) - 1))
            selected_id = _task_id(rows[at])
        else:
            at = 0

        head = _heading(rows, len(all_rows), query)
        detail = _detail(rows[at], _detail_limit(head, note)) if rows else ""
        under = screen.trailer(f"{detail}\n" if detail else "", _KEYS, note)
        screen.paint(_screen_text(rows, at, under, total=len(all_rows), query=query))
        key = screen.getkey()
        note = ""  # it has been read now; the next screen starts clean

        if key == "q":
            return 0
        if key == "/":
            searched = _search(query)
            if searched is not None:
                query = searched
                at = 0
            continue
        if key == "left":
            if query:
                query = ""
                at = 0
                continue
            return 0
        if key == "?":
            _help()
            continue
        if not rows:
            continue

        row = rows[at]
        step = _page_size(head, under)
        if key in ("up", "k"):
            at = max(0, at - 1)
            selected_id = _task_id(rows[at])
            continue
        if key in ("down", "j"):
            at = min(len(rows) - 1, at + 1)
            selected_id = _task_id(rows[at])
            continue
        if key == "home":
            at = 0
            selected_id = _task_id(rows[at])
            continue
        if key == "end":
            at = len(rows) - 1
            selected_id = _task_id(rows[at])
            continue
        if key == "pageup":
            at = max(0, at - step)
            selected_id = _task_id(rows[at])
            continue
        if key == "pagedown":
            at = min(len(rows) - 1, at + step)
            selected_id = _task_id(rows[at])
            continue

        try:
            note = _decide(dsn, row, key)
        except MashuError as error:
            note = screen.danger(f"  ✗ {error}")


def _decide(dsn: str | None, row: dict[str, Any], key: str) -> str:
    """One keystroke carried into the store, or a word saying why it was not."""
    proposal = row.get("proposal")
    task_id: UUID = row["task"]["task_id"]

    if key == "enter":
        if not proposal:
            return screen.warning(
                "  ! left open — nothing is proposed; c, a or s chooses an outcome"
            )
        if proposal["stale"] and not _confirm(
            f"the state was written after this was proposed; close as {proposal['outcome']}?"
        ):
            return screen.warning("  ! left open — stale proposal was not accepted")
        return _close(dsn, row, proposal["outcome"], None)

    if key in _OUTCOME_KEYS:
        outcome = _OUTCOME_KEYS[key]
        return _close(dsn, row, outcome, _reason(row, outcome))

    if key == "w":
        if not proposal:
            return screen.warning("  ! left open — nothing is proposed for this one")
        with db.transaction(dsn) as cur:
            tasks.withdraw_proposal(cur, task_id, actor=ACTOR)
            tasks.touch(cur, task_id, actor=ACTOR)
        return screen.success(
            f"  ✓ {_short(task_id)} is still live; the proposal is gone and the lease renewed"
        )

    if key == "t":
        with db.transaction(dsn) as cur:
            tasks.touch(cur, task_id, actor=ACTOR)
        return screen.success(
            f"  ✓ {_short(task_id)} renewed; nothing said about whether it is finished"
        )

    return ""
