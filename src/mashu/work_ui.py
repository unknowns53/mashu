"""Human-facing browser for tasks and projects."""

from __future__ import annotations

import datetime as dt
import shutil
from typing import Any
from uuid import UUID

from mashu import db, projects, scopes, screen, task_history, tasks
from mashu.errors import DuplicateTaskError, MashuError

ACTOR = "user"
VIEWS = ("active", "dormant", "closed", "projects")
_VIEW_KEYS = {str(number): view for number, view in enumerate(VIEWS, 1)}
_OUTCOMES = {"c": "completed", "a": "abandoned", "s": "superseded"}

_TASK_KEYS = (
    "  1 active  2 dormant  3 closed  4 projects   ↑↓/jk move   Home/End   PgUp/PgDn\n"
    "  / search   ⏎ details   n new   t renew   c close   o reopen   ← back   q leave"
)
_PROJECT_KEYS = (
    "  1 active  2 dormant  3 closed  4 projects   ↑↓/jk move   Home/End   PgUp/PgDn\n"
    "  / search   ⏎ details   n new project   ← back   q leave"
)
_DETAIL_KEYS = "  space read on   any other key goes back"


def _short(value: Any) -> str:
    return str(value)[:8]


def _date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    return str(value) if value else "-"


def _task_id(row: dict[str, Any]) -> UUID:
    return row["task"]["task_id"]


def _row_id(view: str, row: dict[str, Any]) -> UUID:
    return row["project_id"] if view == "projects" else _task_id(row)


def _find(rows: list[dict[str, Any]], wanted: UUID | None, view: str) -> int | None:
    if wanted is None:
        return None
    return next((at for at, row in enumerate(rows) if _row_id(view, row) == wanted), None)


def _load_tasks(dsn: str | None, activity: str) -> list[dict[str, Any]]:
    """Load one task view and the history a person may inspect or search."""
    with db.transaction(dsn) as cur:
        rows = tasks.task_list(cur, activity=activity)
        return [
            task_history.expanded_task(
                cur,
                _task_id(row),
                attempts=True,
                decisions=True,
                artifacts=True,
                checkpoints=True,
            )
            for row in rows
        ]


def _load_projects(dsn: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return projects.list_projects(cur)


def _load(dsn: str | None, view: str) -> list[dict[str, Any]]:
    return _load_projects(dsn) if view == "projects" else _load_tasks(dsn, view)


def _flatten(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [text for item in value.values() for text in _flatten(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _flatten(item)]
    return [str(value)]


def _matches_task(row: dict[str, Any], query: str) -> bool:
    """Match identity, state, proposal, and readable history fields."""
    task = row["task"]
    state = row["state"]
    proposal = row.get("proposal") or {}
    values: list[Any] = [
        task.get("task_id"),
        task.get("name"),
        task.get("project_name"),
        row.get("activity"),
        state,
        proposal.get("outcome"),
        proposal.get("reason"),
    ]
    for history in ("attempts", "decisions", "checkpoints", "artifacts"):
        values.extend(row.get(history) or [])
    needle = query.casefold()
    return any(needle in text.casefold() for value in values for text in _flatten(value))


def _matches_project(row: dict[str, Any], query: str) -> bool:
    needle = query.casefold()
    values = (row.get("project_id"), row.get("name"), row.get("scope_name"))
    return any(needle in str(value).casefold() for value in values if value is not None)


def _filtered(rows: list[dict[str, Any]], view: str, query: str) -> list[dict[str, Any]]:
    match = _matches_project if view == "projects" else _matches_task
    return [row for row in rows if not query or match(row, query)]


def _tabs(view: str) -> str:
    tabs = []
    for number, name in enumerate(VIEWS, 1):
        label = f"{number} {name}"
        tabs.append(screen.bold(f"[{label}]") if name == view else label)
    return "  Work   " + "   ".join(tabs)


def _heading(view: str, shown: int, total: int, query: str) -> str:
    noun = "project" if view == "projects" else "task"
    count = f"{shown}/{total}" if query else str(total)
    suffix = f"  ·  search {screen.accent(repr(query))}" if query else ""
    return f"{_tabs(view)}\n  {count} {view} {noun}(s){suffix}"


def _task_line(row: dict[str, Any], width: int) -> str:
    task = row["task"]
    proposal = row.get("proposal")
    marker = screen.warning("◌") if proposal and proposal["stale"] else "●" if proposal else " "
    project = screen.pad(task["project_name"], 12)
    fixed = f" {marker} {_short(task['task_id'])}  {project}  "
    label = task["name"]
    if row["activity"] == "closed":
        label += f"  [{task['outcome']}]"
    return fixed + screen.clip(label, max(1, width - screen.cells(fixed)))


def _project_line(row: dict[str, Any], width: int) -> str:
    counts = f"{row['n_active']:>2}a {row['n_dormant']:>2}d {row['n_closed']:>2}c"
    fixed = f"   {_short(row['project_id'])}  {counts}  "
    return fixed + screen.clip(row["name"], max(1, width - screen.cells(fixed)))


def _field(label: str, value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple)):
        lines = [f"  {screen.bold(label)}"]
        for item in value:
            lines.extend(screen.wrap(f"• {item}", "    ").splitlines())
        return lines
    prefix = f"  {screen.bold(label)}  "
    wrapped = screen.wrap(str(value), "    ").splitlines()
    if not wrapped:
        return []
    return [prefix + wrapped[0].strip(), *wrapped[1:]]


def _history_lines(row: dict[str, Any]) -> list[str]:
    """All immutable history, in the same groups as `mashu task show`."""
    lines: list[str] = []
    artifacts = row.get("artifacts") or []
    artifacts_by_id = {item["reference_id"]: item for item in artifacts}
    if artifacts:
        lines.append(screen.accent(f"  Artifacts ({len(artifacts)})"))
        for item in artifacts:
            label = f"  ({item['label']})" if item.get("label") else ""
            lines.append(
                f"    {_short(item['reference_id'])}  {_date(item.get('created_at'))}  "
                f"{item.get('kind', 'other')}"
            )
            lines.extend(_field("locator", f"{item.get('locator') or '-'}{label}"))

    checkpoints = row.get("checkpoints") or []
    if checkpoints:
        lines.append(screen.accent(f"  Checkpoints ({len(checkpoints)})"))
        for item in checkpoints:
            dated = f"{_date(item.get('created_at'))}  {item.get('created_by', '-')}"
            lines.append(f"    {screen.dim(dated)}")
            lines.extend(_field("changed", item.get("what_changed")))
            for reference_id in item.get("evidence") or []:
                artifact = artifacts_by_id.get(reference_id)
                evidence = (
                    f"{artifact['kind']}  {artifact['locator']}"
                    if artifact
                    else _short(reference_id)
                )
                lines.extend(_field("evidence", evidence))

    attempts = row.get("attempts") or []
    if attempts:
        lines.append(screen.accent(f"  Attempts ({len(attempts)})"))
        for item in attempts:
            dated = f"{_date(item.get('created_at'))}  {item.get('created_by', '-')}"
            lines.append(f"    {screen.dim(dated)}")
            lines.extend(_field("attempt", item.get("attempt")))
            for field in ("result", "reason", "next"):
                lines.extend(_field(field, item.get(field)))

    decisions = row.get("decisions") or []
    if decisions:
        lines.append(screen.accent(f"  Decisions ({len(decisions)})"))
        for item in decisions:
            dated = f"{_date(item.get('created_at'))}  {item.get('created_by', '-')}"
            lines.append(f"    {screen.dim(dated)}")
            lines.extend(_field("decision", item.get("decision")))
            lines.extend(_field("reason", item.get("reason")))
            if item.get("supersedes_id"):
                lines.extend(_field("supersedes", _short(item["supersedes_id"])))
    return lines


def _task_detail(row: dict[str, Any]) -> str:
    task, state = row["task"], row["state"]
    lines = [
        screen.bold(f"  {_short(task['task_id'])}  {task['name']}"),
        f"  {task['project_name']}  ·  {row['heading']}",
        f"  status  {task['status']} / {row['activity']}",
    ]
    if task["status"] == "closed":
        lines.extend(_field("outcome", task.get("outcome")))
        lines.extend(_field("close reason", task.get("close_reason")))
    proposal = row.get("proposal")
    if proposal:
        stale = "  STALE" if proposal["stale"] else ""
        lines.append(
            screen.warning(
                f"  Proposal  {proposal['outcome']} by {proposal['proposed_by']} "
                f"on {proposal['on_date']}{stale}"
            )
        )
        lines.extend(_field("proposal reason", proposal["reason"]))
    lines.append("")
    for label, key in (
        ("Goal", "goal"),
        ("Approach", "approach"),
        ("Status", "status_text"),
        ("Open questions", "open_questions"),
        ("Blockers", "blockers"),
        ("Next actions", "next_actions"),
    ):
        lines.extend(_field(label, state.get(key)))
    if all(not state.get(key) for key in tasks.EDITABLE_FIELDS):
        lines.append(screen.dim("  (no current state written)"))
    history = _history_lines(row)
    if history:
        lines.extend(("", screen.bold("  History"), *history))
    return "\n".join(lines)


def _project_detail(row: dict[str, Any]) -> str:
    lines = [
        screen.bold(f"  {_short(row['project_id'])}  {row['name']}"),
        f"  scope  {row.get('scope_name') or '-'}",
        f"  tasks  {row['n_active']} active  ·  {row['n_dormant']} dormant  "
        f"·  {row['n_closed']} closed",
        f"  created  {_date(row.get('created_at'))}",
    ]
    return "\n".join(lines)


def _detail(row: dict[str, Any], view: str) -> str:
    return _project_detail(row) if view == "projects" else _task_detail(row)


def _preview(row: dict[str, Any], view: str, limit: int) -> str:
    lines = _detail(row, view).splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    marker = screen.dim(f"  … {len(lines) - limit + 1} more; ⏎ reads all")
    return "\n".join([*lines[: max(1, limit - 1)], marker])


def _preview_limit(head: str, keys: str, note: str) -> int:
    height = shutil.get_terminal_size((screen.WIDTH, 24)).lines
    fixed = screen.height_of(head) + screen.height_of(keys) + screen.height_of(note) + 7
    return max(3, min(14, height - fixed))


def _screen_text(
    rows: list[dict[str, Any]],
    at: int,
    view: str,
    under: str,
    *,
    total: int,
    query: str,
) -> str:
    head = _heading(view, len(rows), total, query)
    width = screen.terminal_width() - 1
    line = _project_line if view == "projects" else _task_line
    listed = [line(row, width) for row in rows]
    if rows:
        listed[at] = screen.selected(listed[at])
    else:
        listed = [screen.dim("  no matches; / searches again, ← clears the search")]
    return screen.list_screen(head, listed, at, under)


def _search(current: str) -> str | None:
    prompt = f"  search [{current}; empty clears]: " if current else "  search [empty clears]: "
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _readline(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _confirm(question: str) -> bool:
    answer = _readline(f"  {question} [y/N]: ")
    return bool(answer and answer.casefold().startswith("y"))


def _show_detail(row: dict[str, Any], view: str) -> None:
    body, offset = _detail(row, view), 0
    while True:
        page, more = screen.paged(body, offset, _DETAIL_KEYS)
        screen.paint(page + "\n\n" + _DETAIL_KEYS)
        key = screen.getkey()
        if key != "space" or not more:
            return
        offset = more


def _duplicate_candidates(error: DuplicateTaskError) -> str:
    lines = ["  Similar open tasks:"]
    for row in error.candidates:
        task = row["task"]
        lines.append(f"    {_short(task['task_id'])}  {task['name']}  [{row['activity']}]")
    return "\n".join(lines)


def _create_task(dsn: str | None) -> tuple[str, UUID | None]:
    name = _readline("  task name [empty cancels]: ")
    if not name:
        return screen.warning("  ! task creation cancelled"), None
    project = _readline("  project [empty cancels]: ")
    if not project:
        return screen.warning("  ! task creation cancelled"), None
    goal = _readline("  goal [optional]: ")
    if goal is None:
        return screen.warning("  ! task creation cancelled"), None
    try:
        with db.transaction(dsn) as cur:
            created = tasks.task_create(
                cur, project=project, name=name, goal=goal or None, actor=ACTOR
            )
    except DuplicateTaskError as error:
        print(_duplicate_candidates(error))
        if not _confirm("create it anyway?"):
            return screen.warning("  ! left existing tasks alone"), None
        with db.transaction(dsn) as cur:
            created = tasks.task_create(
                cur, project=project, name=name, goal=goal or None, actor=ACTOR, force=True
            )
    task_id = _task_id(created)
    return screen.success(f"  ✓ created task  {_short(task_id)}"), task_id


def _create_project(dsn: str | None) -> tuple[str, UUID | None]:
    name = _readline("  project name [empty cancels]: ")
    if not name:
        return screen.warning("  ! project creation cancelled"), None
    scope_name = _readline("  scope [optional]: ")
    if scope_name is None:
        return screen.warning("  ! project creation cancelled"), None
    with db.transaction(dsn) as cur:
        scope_id = scopes.require_scope(cur, scope_name)["scope_id"] if scope_name else None
        created = projects.create_project(cur, name=name, scope_id=scope_id, actor=ACTOR)
    note = screen.success(f"  ✓ created project  {_short(created['project_id'])}")
    return note, created["project_id"]


def _touch(dsn: str | None, row: dict[str, Any]) -> str:
    task_id = _task_id(row)
    with db.transaction(dsn) as cur:
        tasks.touch(cur, task_id, actor=ACTOR)
    return screen.success(f"  ✓ renewed  {_short(task_id)}")


def _close_task(dsn: str | None, row: dict[str, Any]) -> str:
    proposal = row.get("proposal")
    outcome: str | None = None
    if proposal:
        choice = _readline(
            f"  accept proposed {proposal['outcome']}? [y / c completed / a abandoned / "
            "s superseded / empty cancels]: "
        )
        if choice is None or not choice:
            return screen.warning("  ! left open")
        outcome = (
            proposal["outcome"]
            if choice.casefold().startswith("y")
            else _OUTCOMES.get(choice[0].lower())
        )
    else:
        choice = _readline(
            "  close as [c completed / a abandoned / s superseded / empty cancels]: "
        )
        if choice is None or not choice:
            return screen.warning("  ! left open")
        outcome = _OUTCOMES.get(choice[0].lower())
    if outcome is None:
        return screen.warning("  ! left open — choose c, a, or s")
    if (
        proposal
        and proposal["stale"]
        and not _confirm("the state changed after this proposal; close anyway?")
    ):
        return screen.warning("  ! left open — stale proposal was not confirmed")

    accepted = bool(
        proposal and outcome == proposal["outcome"] and choice.casefold().startswith("y")
    )
    reason: str | None = None
    if not accepted:
        reason = _readline("  reason [optional]: ")
        if reason is None:
            return screen.warning("  ! left open")
        reason = reason or None
    task_id = _task_id(row)
    with db.transaction(dsn) as cur:
        tasks.close(cur, task_id, outcome=outcome, reason=reason, actor=ACTOR)
    return screen.success(f"  ✓ closed  {_short(task_id)}  {outcome}")


def _reopen(dsn: str | None, row: dict[str, Any]) -> str:
    task_id = _task_id(row)
    with db.transaction(dsn) as cur:
        tasks.reopen(cur, task_id, actor=ACTOR)
    return screen.success(f"  ✓ reopened  {_short(task_id)}")


def _page_size(head: str, under: str) -> int:
    return max(1, screen.room_under(f"{head}\n\n\n{under}"))


@screen.fullscreen
def run(dsn: str | None = None, *, initial_view: str = "active") -> int:
    """Browse and perform the person-owned parts of task and project work."""
    if initial_view not in VIEWS:
        raise ValueError(f"unknown work view {initial_view!r}")
    view, at, query, note = initial_view, 0, "", ""
    selected: dict[str, UUID | None] = {name: None for name in VIEWS}

    while True:
        try:
            all_rows = _load(dsn, view)
        except MashuError as error:
            print(error)
            return 1
        rows = _filtered(all_rows, view, query)
        preserved = _find(rows, selected[view], view)
        if preserved is not None:
            at = preserved
        elif rows:
            at = max(0, min(at, len(rows) - 1))
            selected[view] = _row_id(view, rows[at])
        else:
            at = 0

        keys = _PROJECT_KEYS if view == "projects" else _TASK_KEYS
        head = _heading(view, len(rows), len(all_rows), query)
        preview = _preview(rows[at], view, _preview_limit(head, keys, note)) if rows else ""
        under = screen.trailer(f"{preview}\n" if preview else "", keys, note)
        screen.paint(_screen_text(rows, at, view, under, total=len(all_rows), query=query))
        key = screen.getkey()
        note = ""

        if key == "q":
            return 0
        if key in _VIEW_KEYS:
            view, at, query = _VIEW_KEYS[key], 0, ""
            continue
        if key == "/":
            searched = _search(query)
            if searched is not None:
                query, at = searched, 0
            continue
        if key == "left":
            if query:
                query, at = "", 0
                continue
            return 0

        if key == "n":
            try:
                if view == "projects":
                    note, made = _create_project(dsn)
                    if made:
                        selected["projects"] = made
                else:
                    note, made = _create_task(dsn)
                    if made:
                        view, query, at = "active", "", 0
                        selected["active"] = made
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
            continue
        if not rows:
            continue

        row = rows[at]
        step = _page_size(head, under)
        if key in ("up", "k"):
            at = max(0, at - 1)
        elif key in ("down", "j"):
            at = min(len(rows) - 1, at + 1)
        elif key == "home":
            at = 0
        elif key == "end":
            at = len(rows) - 1
        elif key == "pageup":
            at = max(0, at - step)
        elif key == "pagedown":
            at = min(len(rows) - 1, at + step)
        elif key == "enter":
            _show_detail(row, view)
            continue
        else:
            try:
                if view in ("active", "dormant") and key == "t":
                    note = _touch(dsn, row)
                elif view in ("active", "dormant") and key == "c":
                    note = _close_task(dsn, row)
                elif view == "closed" and key == "o":
                    note = _reopen(dsn, row)
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
            continue
        selected[view] = _row_id(view, rows[at])
