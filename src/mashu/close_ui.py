"""Interactive screen for reviewing and closing open tasks."""

from __future__ import annotations

import datetime as dt
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
    "  ↑↓ move   ⏎ take the proposal   c completed   a abandoned   s superseded\n"
    "  w drop the proposal   t still live   ? help   q leave"
)

_HELP = """
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
    return "◌" if proposal["stale"] else "●"


def _line(row: dict[str, Any], width: int) -> str:
    """One task, as much of it as the terminal is wide."""
    task = row["task"]
    return screen.clip(
        f" {_mark(row)} {_short(task['task_id'])}  {screen.pad(task['project_name'], 10)} "
        f"{screen.pad(_ACTIVITY[row['activity']], 4)} {row['age_days']:>3}d  {task['name']}",
        width - 1,
    )


def _detail(row: dict[str, Any]) -> str:
    """What is being decided about, under the list and above the keys."""
    task = row["task"]
    head = f"  ── {_short(task['task_id'])}  {task['name']}"
    proposal = row.get("proposal")
    if not proposal:
        body = row["state"]["status_text"] or row["state"]["goal"] or "(no state written)"
        return f"{head}\n  {row['heading']}\n{screen.wrap(body, '  ')}"
    said = f"  proposed {proposal['outcome']} by {proposal['proposed_by']} on {proposal['on_date']}"
    if proposal["stale"]:
        said += f"; the state has been written since, as of {row['as_of']}"
    return f"{head}\n{said}\n{screen.wrap(proposal['reason'], '  ')}"


def _screen_text(rows: list[dict[str, Any]], at: int, under: str) -> str:
    """The whole screen: how many are open, the list, the cursor's task, the keys."""
    width = screen.terminal_width()
    standing = sum(1 for row in rows if row.get("proposal"))
    head = f"  {len(rows)} open task(s)" + (f", {standing} proposed closed" if standing else "")
    lines = []
    for number, row in enumerate(rows):
        line = _line(row, width)
        lines.append(f"\x1b[1m▸{line[1:]}\x1b[0m" if number == at else line)
    return screen.list_screen(head, lines, at, under)


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
    return f"  closed  {_short(task_id)}  {outcome}"


def _open_tasks(dsn: str | None, project: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return _order(tasks.task_list(cur, project=project, activity="open"))


def run(dsn: str | None = None, *, project: str | None = None) -> int:
    """Work the list: the open tasks, and one decision at a time against them."""
    at, note = 0, ""
    while True:
        try:
            rows = _open_tasks(dsn, project)
        except MashuError as error:
            print(error)
            return 1
        if not rows:
            if note:
                print(note)
            print(_NOTHING)
            return 0
        at = max(0, min(at, len(rows) - 1))
        row = rows[at]

        under = screen.trailer(f"{_detail(row)}\n", _KEYS, note)
        screen.paint(_screen_text(rows, at, under))
        key = screen.getkey()
        note = ""  # it has been read now; the next screen starts clean

        if key == "q":
            return 0
        if key == "?":
            _help()
            continue
        if key in ("up", "pageup"):
            at = max(0, at - 1)
            continue
        if key in ("down", "pagedown"):
            at = min(len(rows) - 1, at + 1)
            continue

        try:
            note = _decide(dsn, row, key)
        except MashuError as error:
            note = f"  {error}"


def _decide(dsn: str | None, row: dict[str, Any], key: str) -> str:
    """One keystroke carried into the store, or a word saying why it was not."""
    proposal = row.get("proposal")
    task_id: UUID = row["task"]["task_id"]

    if key == "enter":
        if not proposal:
            return "  nothing is proposed for this one; c, a or s chooses an outcome"
        if proposal["stale"] and not _confirm(
            f"the state was written after this was proposed; close as {proposal['outcome']}?"
        ):
            return "  left open"
        return _close(dsn, row, proposal["outcome"], None)

    if key in _OUTCOME_KEYS:
        outcome = _OUTCOME_KEYS[key]
        return _close(dsn, row, outcome, _reason(row, outcome))

    if key == "w":
        if not proposal:
            return "  nothing is proposed for this one"
        with db.transaction(dsn) as cur:
            tasks.withdraw_proposal(cur, task_id, actor=ACTOR)
            tasks.touch(cur, task_id, actor=ACTOR)
        return f"  {_short(task_id)} is still live; the proposal is gone and the lease renewed"

    if key == "t":
        with db.transaction(dsn) as cur:
            tasks.touch(cur, task_id, actor=ACTOR)
        return f"  {_short(task_id)} renewed; nothing said about whether it is finished"

    return ""
