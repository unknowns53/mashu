"""Interactive screen for reviewing and closing open tasks."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mashu import db, screen, task_actions, tasks
from mashu.errors import MashuError


@dataclass(frozen=True)
class CloseScreenResult:
    exit_code: int
    note: str = ""


#: Only a person reaches this screen; the CLI is the only way in.
ACTOR = "user"

#: The three outcomes, on the letters they start with.
_OUTCOME_KEYS = {"c": "completed", "a": "abandoned", "s": "superseded"}

#: Wide enough for the longer of the two lease words, active and dormant.
_ACTIVITY_WIDTH = 7

#: The project column grows to the longest name shown, up to this share of the width,
#: so the task name keeps most of the line.
_PROJECT_SHARE = 5

_KEYS = (
    "  ↑↓/jk move   Home/End   PgUp/PgDn   / search   ⏎ take   y take all   q quit\n"
    "  c completed   a abandoned   s superseded   w drop   t renew   ? help   ← back"
)

_ALL_KEYS = "  y close them   any other key leaves them open   q quit"

_EMPTY_KEYS = "  ← back   q quit"

_HELP = """
  ↑/↓ or j/k move one task. Home and End jump to the first and last task;
     Page Up and Page Down move by one visible screenful.

  /  search task id, name, project, activity, current state, or proposal grounds.
     Search is case-insensitive. Submit an empty search to show everything again.

  ← or Esc clears an active search; with no search it goes back.

  q  quit Mashu.

  ⏎  close the task on the proposal printed under the list: its outcome and
     its grounds, recorded as your decision. Only where one stands, and where
     the state has moved since it was written, you are asked once more first.

  c  close as completed. a and s close as abandoned and superseded. Each asks
     for a reason and takes an empty one — except where the proposal already
     names that same outcome, when an empty line keeps its grounds.

  w  drop the proposal and say the work is still live. The task stays open
     with its lease renewed, and the agent that proposed it will see that it
     no longer stands.

  t  renew the lease without deciding anything. A dormant task becomes
     active; nothing is said about whether it is finished.

  ●  before a task: a close proposal stands for it.
  ◌  before a task: a close proposal whose task state was written after it.
     ⏎ asks once more before taking it.

  y  take every standing proposal (●) at once. One screen lists the tasks
     with their proposed outcomes and asks once; y there closes each as ⏎
     would. Stale proposals (◌) stay open for review one by one. With a
     search, only the tasks it shows are taken.
"""

_HELP_KEYS = "  space read on   q quit   any other key goes back"

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


def _project_width(rows: list[dict[str, Any]], width: int) -> int:
    longest = max((screen.cells(row["task"]["project_name"]) for row in rows), default=0)
    return max(4, min(longest + 1, width // _PROJECT_SHARE))


def _line(row: dict[str, Any], width: int, project_width: int = 10) -> str:
    """One task, as much of it as the terminal is wide."""
    task = row["task"]
    project = screen.pad(task["project_name"], project_width)
    activity = screen.pad(row["activity"], _ACTIVITY_WIDTH)
    plain_fixed = f"   {_short(task['task_id'])}  {project} {activity}"
    styled_activity = (
        screen.success(activity) if row["activity"] == "active" else screen.dim(activity)
    )
    fixed = f" {_mark(row)} {_short(task['task_id'])}  {project} {styled_activity}"
    suffix = f" {row['age_days']:>3}d  {task['name']}"
    room = max(1, width - screen.cells(plain_fixed))
    return f"{fixed}{screen.clip(suffix, room)}"


def _field(label: str, value: Any, label_width: int, *, value_style=None) -> str:
    """One compact, readable state field which cannot wrap past the screen."""
    if isinstance(value, (list, tuple)):
        text = "  •  ".join(str(item) for item in value)
    else:
        text = " ".join(str(value).splitlines())
    shown_label = screen.pad(label, label_width)
    available = max(1, screen.terminal_width() - 2 - label_width)
    shown = screen.clip(text, available)
    if value_style is not None:
        shown = value_style(shown)
    return f"  {screen.bold(shown_label)}{shown}"


def _detail(row: dict[str, Any]) -> str:
    """The selected task's decision context, cut to the room below the list."""
    task = row["task"]
    title_prefix = f"  ── {_short(task['task_id'])}  "
    title = screen.clip(task["name"], max(1, screen.terminal_width() - screen.cells(title_prefix)))
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
    state = row["state"]
    state_fields = [
        (label, value)
        for label, value in (
            ("goal", state.get("goal")),
            ("status", state.get("status_text")),
            ("approach", state.get("approach")),
            ("open questions", state.get("open_questions")),
            ("blockers", state.get("blockers")),
            ("next actions", state.get("next_actions")),
        )
        if value
    ]
    labels = [label for label, _ in state_fields]
    if proposal:
        labels.append("reason")
        if proposal["stale"]:
            labels.append("STALE")
    # Two spaces always separate the longest label from its value.
    label_width = max((screen.cells(label) for label in labels), default=0) + 2

    if proposal:
        said = (
            f"PROPOSAL  proposed {proposal['outcome']} by "
            f"{proposal['proposed_by']} on {proposal['on_date']}"
        )
        lines.append(screen.accent(f"  {said}"))
        lines.append(_field("reason", proposal["reason"], label_width, value_style=screen.accent))
        if proposal["stale"]:
            stale = f"state was written since the proposal, as of {row['as_of']}"
            lines.append(screen.warning(f"  {screen.pad('STALE', label_width)}{stale}"))

    for label, value in state_fields:
        lines.append(_field(label, value, label_width))
    if len(lines) == 2 and not proposal:
        lines.append(screen.dim("  (no state written)"))

    return "\n".join(lines)


def _heading(rows: list[dict[str, Any]], total: int, query: str) -> str:
    """The screen's name, then the counts, which also say what the two marks mean."""
    standing = sum(1 for row in rows if row.get("proposal"))
    stale = sum(1 for row in rows if row.get("proposal") and row["proposal"]["stale"])
    if query:
        head = f"  {len(rows)}/{total} open task(s)  ·  search {screen.accent(repr(query))}"
    else:
        head = f"  {total} open task(s)"
    if standing - stale:
        head += f"  ·  {screen.accent('●')} {standing - stale} proposed closed"
    if stale:
        head += f"  ·  {screen.warning('◌')} {stale} stale proposal(s)"
    return f"{screen.bold(screen.accent('Close tasks'))}\n{head}"


def _screen_text(
    rows: list[dict[str, Any]],
    at: int,
    note: str = "",
    *,
    total: int | None = None,
    query: str = "",
) -> str:
    """The whole screen: how many are open, the list, the cursor's task, the keys."""
    width = screen.terminal_width()
    head = _heading(rows, len(rows) if total is None else total, query)
    project_width = _project_width(rows, width - 1)
    lines = []
    for number, row in enumerate(rows):
        line = _line(row, width - 1, project_width)
        lines.append(screen.selected(line) if number == at else line)
    if not rows:
        lines.append(screen.dim("  no tasks match; / searches again, ← clears the search"))
    detail = _detail(rows[at]) if rows else ""
    return screen.list_with_preview(head, lines, at, detail, _KEYS, note)


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


def _help() -> None:
    """Paint the explanation and wait, so the screen itself can stay short."""
    offset = 0
    body = _HELP.strip("\n")
    while True:
        page, more = screen.paged(f"  what each key does\n\n\n{body}", offset, _HELP_KEYS)
        screen.paint(page + "\n" + _HELP_KEYS)
        key = screen.getkey()
        if key == "q":
            raise screen.Quit()
        if key != "space" or not more:
            return
        offset = more


def _finish(note: str) -> int:
    """Say nothing is open where it can be read, and wait there if that is a screen."""
    if not screen.in_alternate_screen():
        if note:
            print(note)
        print(_NOTHING)
        return 0
    shown = screen.trailer(screen.bold(screen.accent("Close tasks")), f"  {_NOTHING}", note)
    screen.paint(f"{shown}\n\n{_EMPTY_KEYS}")
    if screen.getkey() == "q":
        raise screen.Quit()
    return 0


def _reason(row: dict[str, Any], outcome: str) -> screen.InputResult:
    """The grounds for a close, where an empty line is an answer."""
    proposal = row.get("proposal")
    if proposal and proposal["outcome"] == outcome:
        prompt = f"  reason [⏎ keeps: {screen.clip(proposal['reason'], 48)}]: "
    else:
        prompt = "  reason [⏎ for none]: "
    return screen.editline(prompt, "")


def _confirm(question: str) -> bool:
    try:
        return input(f"  {question} [y/N]: ").strip().lower().startswith("y")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _close(
    dsn: str | None,
    row: dict[str, Any],
    outcome: str,
    reason: str | None,
    *,
    accept_proposal: bool = False,
) -> str:
    task_id = row["task"]["task_id"]
    with db.transaction(dsn) as cur:
        if accept_proposal:
            task_actions.accept_close_proposal(
                cur,
                task_id,
                expected_proposal=row["proposal"],
                expected_state=row["state"]["updated_at"],
                actor=ACTOR,
            )
        else:
            task_actions.close_task(cur, task_id, outcome=outcome, actor=ACTOR, reason=reason)
    return screen.success(f"  ✓ closed  {_short(task_id)}  {outcome}")


def _open_tasks(
    dsn: str | None, project: str | None, task_id: UUID | None = None
) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        rows = tasks.task_list(cur, project=project, activity="open")
    if task_id is not None:
        rows = [row for row in rows if _task_id(row) == task_id]
    return _order(rows)


@screen.fullscreen
def run(
    dsn: str | None = None,
    *,
    project: str | None = None,
    task_id: UUID | None = None,
    return_result: bool = False,
) -> int | CloseScreenResult:
    """Work the list, optionally returning one-task feedback to a parent screen."""
    if return_result and task_id is None:
        raise ValueError("a returned close result needs one task")
    at, note, query = 0, "", ""
    selected_id = task_id
    while True:
        try:
            all_rows = _open_tasks(dsn, project, task_id)
        except MashuError as error:
            if return_result:
                return CloseScreenResult(1, screen.danger(f"  ✗ {error}"))
            print(error)
            return 1
        if not all_rows:
            if return_result:
                return CloseScreenResult(0, note or _NOTHING)
            return _finish(note)
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
        screen.paint(_screen_text(rows, at, note, total=len(all_rows), query=query))
        key = screen.getkey()
        note = ""  # it has been read now; the next screen starts clean

        if key == "q":
            raise screen.Quit()
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
            if return_result:
                return CloseScreenResult(0, screen.warning("  ! left open — close cancelled"))
            return 0
        if key == "?":
            _help()
            continue
        if not rows:
            continue

        row = rows[at]
        step = max(1, screen.list_room(head, _KEYS, len(rows)))
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

        if key == "y":
            note = _take_all(dsn, rows)
        else:
            try:
                note = _decide(dsn, row, key)
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
        if task_id is not None:
            if return_result:
                return CloseScreenResult(0, note)
            if note:
                print(note)
            return 0


def _standing(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The rows whose proposal can be taken without a second look, and the stale count."""
    taken = [row for row in rows if row.get("proposal") and not row["proposal"]["stale"]]
    stale = sum(1 for row in rows if row.get("proposal") and row["proposal"]["stale"])
    return taken, stale


def _left_for_review(stale: int) -> str:
    return f"  ·  {stale} stale proposal(s) left for review one by one" if stale else ""


def _all_screen(taken: list[dict[str, Any]], stale: int) -> str:
    """What y would close, as much of it as fits, with the count it cannot show."""
    head = (
        f"{screen.bold(screen.accent('Close tasks'))}\n"
        f"  close {len(taken)} task(s) on their standing proposals"
    )
    stale_line = screen.warning(f"  ◌ {stale} stale proposal(s) stay open") if stale else ""
    under = "\n" + screen.trailer(stale_line, _ALL_KEYS)
    width = screen.terminal_width() - 1
    lines = []
    for row in taken:
        task = row["task"]
        fixed = f"  {_short(task['task_id'])}  {screen.pad(row['proposal']['outcome'], 10)}  "
        room = max(1, width - screen.cells(fixed))
        lines.append(f"{fixed}{screen.clip(task['name'], room)}")
    # The blank line under the heading is not counted by room_under.
    room = max(1, screen.room_under(head, under) - 1)
    if len(lines) > room:
        shown = lines[: room - 1]
        lines = [*shown, screen.dim(f"  … and {len(taken) - len(shown)} more")]
    return "\n".join((head, "", *lines, under))


def _take_all(dsn: str | None, rows: list[dict[str, Any]]) -> str:
    """Close every standing proposal in the list after one confirmation, stopping at a failure."""
    taken, stale = _standing(rows)
    if not taken:
        return screen.warning("  ! nothing taken — no standing proposal" + _left_for_review(stale))
    screen.paint(_all_screen(taken, stale))
    key = screen.getkey()
    if key == "q":
        raise screen.Quit()
    if key != "y":
        return screen.warning("  ! left open — nothing closed")
    closed = 0
    for row in taken:
        proposal = row["proposal"]
        try:
            _close(dsn, row, proposal["outcome"], proposal["reason"], accept_proposal=True)
        except MashuError as error:
            failed = _short(row["task"]["task_id"])
            return screen.danger(
                f"  ✗ {failed} not closed: {error}  ·  {closed} of {len(taken)} closed before it"
            )
        closed += 1
    return screen.success(f"  ✓ closed {closed} task(s)" + _left_for_review(stale))


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
        return _close(dsn, row, proposal["outcome"], proposal["reason"], accept_proposal=True)

    if key in _OUTCOME_KEYS:
        outcome = _OUTCOME_KEYS[key]
        answer = _reason(row, outcome)
        if isinstance(answer, screen.Cancelled):
            return screen.warning("  ! left open — close cancelled")
        reason = answer.text or (
            proposal["reason"] if proposal and proposal["outcome"] == outcome else None
        )
        return _close(dsn, row, outcome, reason)

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

    return screen.warning("  ! no action — choose Enter, y, c, a, s, w or t")
