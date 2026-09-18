"""Interactive browser and editor for durable and temporary memory."""

from __future__ import annotations

import datetime as dt
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from mashu import db, memories, scopes, screen, temporary
from mashu.errors import MashuError, RetiredConflictError

ACTOR = "user"

_VIEWS = {"1": "active", "2": "retired", "3": "temporary"}
_TABS = "  1 active   2 retired   3 temporary"
_KEYS = (
    "  ↑↓/jk move   Home/End   PgUp/PgDn   / search   ⏎ why   ← back   q leave\n"
    "  n remember   p temporary   e revise   r retire   d delivery"
)
_ITEM_KEYS = "  space read on   b page back   ← list   q leave"


def _short(value: Any) -> str:
    return str(value)[:8]


def _date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    return str(value or "-")


def _row_id(row: dict[str, Any], view: str) -> UUID:
    return row["context_id" if view == "temporary" else "memory_id"]


def _rows(dsn: str | None, view: str) -> list[dict[str, Any]]:
    """Read one complete view, including the scope names people recognize."""
    with db.transaction(dsn) as cur:
        if view == "temporary":
            cur.execute(
                """
                SELECT t.*, s.name AS scope_name
                FROM temporary_context t LEFT JOIN scope s ON s.scope_id = t.scope_id
                WHERE t.expires_at > now()
                ORDER BY t.expires_at, t.context_id
                """
            )
        else:
            cur.execute(
                """
                SELECT m.*, s.name AS scope_name
                FROM memory m LEFT JOIN scope s ON s.scope_id = m.scope_id
                WHERE m.status = %s
                ORDER BY m.delivery, s.name NULLS FIRST, m.created_at, m.memory_id
                """,
                (view,),
            )
        return cur.fetchall()


def _memory_record(dsn: str | None, memory_id: UUID) -> dict[str, Any]:
    """Read a memory together with the evidence and wording history behind it."""
    with db.transaction(dsn) as cur:
        cur.execute(
            """
            SELECT m.*, s.name AS scope_name
            FROM memory m LEFT JOIN scope s ON s.scope_id = m.scope_id
            WHERE m.memory_id = %s
            """,
            (memory_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise MashuError(f"no memory {memory_id}")

        evidence_ids = list(row.get("evidence") or [])
        evidence: list[dict[str, Any]] = []
        if evidence_ids:
            cur.execute(
                """
                SELECT ledger_id, kind, what, prevention, created_at, created_by
                FROM ledger WHERE ledger_id = ANY(%s)
                """,
                (evidence_ids,),
            )
            by_id = {item["ledger_id"]: item for item in cur.fetchall()}
            evidence = [by_id[item] for item in evidence_ids if item in by_id]

        cur.execute(
            """
            SELECT content, note, actor, created_at
            FROM memory_revision
            WHERE memory_id = %s
            ORDER BY created_at, revision_id
            """,
            (memory_id,),
        )
        return {**row, "evidence_rows": evidence, "revision_rows": cur.fetchall()}


def _delivery(row: dict[str, Any]) -> str:
    delivery = row["delivery"]
    if delivery == "guard":
        return f"guard:{row.get('guard_action') or '-'}"
    return delivery


def _line(row: dict[str, Any], view: str, width: int) -> str:
    scope = row.get("scope_name") or "-"
    if view == "temporary":
        fixed = f"   {_short(row['context_id'])}  {screen.pad(scope, 14)}  "
        expiry = _date(row["expires_at"])
        return screen.clip(f"{fixed}{expiry}  {row['content']}", width)
    fixed = (
        f"   {_short(row['memory_id'])}  {screen.pad(_delivery(row), 16)} {screen.pad(scope, 14)}  "
    )
    return screen.clip(f"{fixed}{row['content']}", width)


def _field(label: str, value: Any) -> str:
    prefix = f"  {label:<10}"
    text = " ".join(str(value or "-").splitlines())
    return f"{screen.bold(prefix)}{screen.clip(text, max(1, screen.text_width() - 12))}"


def _detail(row: dict[str, Any], view: str, limit: int = 8) -> str:
    """The selected row's complete decision fields, bounded below the list."""
    if view == "temporary":
        lines = [
            screen.bold(f"  ── temporary {_short(row['context_id'])}"),
            _field("scope", row.get("scope_name") or "-"),
            _field("expires", _date(row.get("expires_at"))),
            screen.bold("  content"),
            screen.wrap(row.get("content") or "", indent="    "),
        ]
    else:
        lines = [
            screen.bold(f"  ── {view} memory {_short(row['memory_id'])}"),
            _field("delivery", row.get("delivery")),
            _field("scope", row.get("scope_name") or "-"),
            _field("guard", row.get("guard_action") or "-"),
        ]
        if view == "retired":
            lines.append(_field("retired", _date(row.get("retired_at"))))
            lines.append(_field("reason", row.get("retire_reason") or "-"))
        lines.extend(
            [
                screen.bold("  content"),
                screen.wrap(row.get("content") or "", indent="    "),
            ]
        )

    physical: list[str] = []
    for line in lines:
        physical.extend(line.splitlines() or [""])
    if len(physical) > limit:
        hidden = len(physical) - limit + 1
        physical = physical[: limit - 1] + [screen.dim(f"  … {hidden} more detail line(s)")]
    return "\n".join(physical)


def _full_detail(row: dict[str, Any]) -> str:
    """The complete account of what this rule says and why it exists."""
    status = row["status"]
    lines = [
        screen.bold(f"  memory {_short(row['memory_id'])}  {status}"),
        screen.dim(f"  {_date(row.get('created_at'))}  by {row.get('created_by') or '-'}"),
        "",
        _field("delivery", row.get("delivery")),
        _field("scope", row.get("scope_name") or "-"),
        _field("guard", row.get("guard_action") or "-"),
    ]
    if status == "retired":
        lines.extend(
            [
                _field("retired", _date(row.get("retired_at"))),
                screen.bold("  retirement reason"),
                screen.wrap(row.get("retire_reason") or "-", indent="    "),
            ]
        )
    lines.extend([screen.bold("  content"), screen.wrap(row["content"], indent="    "), ""])

    lines.append(screen.accent("  evidence"))
    evidence = row.get("evidence_rows") or []
    if not evidence:
        lines.append(screen.dim("    (none)"))
    for item in evidence:
        lines.append(
            f"    {screen.bold(item['kind'])}  {_date(item.get('created_at'))}  "
            f"by {item.get('created_by') or '-'}"
        )
        lines.append(screen.wrap(f"what: {item.get('what') or '-'}", indent="      "))
        lines.append(screen.wrap(f"prevention: {item.get('prevention') or '-'}", indent="      "))

    lines.extend(["", screen.accent("  revisions")])
    revisions = row.get("revision_rows") or []
    if not revisions:
        lines.append(screen.dim("    (none)"))
    for revision in revisions:
        lines.append(f"    {_date(revision.get('created_at'))}  by {revision.get('actor') or '-'}")
        lines.append(screen.wrap(revision.get("content") or "-", indent="      "))
        lines.append(screen.wrap(f"note: {revision.get('note') or '-'}", indent="      "))
    return "\n".join(lines)


def _full_temporary(row: dict[str, Any]) -> str:
    """A temporary condition without clipping its content."""
    return "\n".join(
        [
            screen.bold(f"  temporary {_short(row['context_id'])}"),
            screen.dim(f"  expires {_date(row.get('expires_at'))}"),
            "",
            _field("scope", row.get("scope_name") or "-"),
            screen.bold("  content"),
            screen.wrap(row.get("content") or "", indent="    "),
        ]
    )


def _heading(view: str, shown: int, total: int, query: str) -> str:
    name = "temporary context" if view == "temporary" else f"{view} memories"
    count = f"{shown}/{total}" if query else str(total)
    suffix = f"  ·  search {screen.accent(repr(query))}" if query else ""
    return f"{_TABS}\n\n  {screen.bold(name)}  {count}{suffix}"


def _screen_text(
    rows: list[dict[str, Any]],
    at: int,
    view: str,
    note: str,
    *,
    total: int,
    query: str,
) -> str:
    head = _heading(view, len(rows), total, query)
    rendered = [
        screen.selected(_line(row, view, screen.terminal_width() - 1))
        if number == at
        else _line(row, view, screen.terminal_width() - 1)
        for number, row in enumerate(rows)
    ]
    if not rows:
        rendered.append(screen.dim("  no rows match; / searches again, ← clears the search"))
    detail = _detail(rows[at], view) if rows else ""
    return screen.list_screen(head, rendered, at, screen.trailer(detail, _KEYS, note))


def _matches(row: dict[str, Any], view: str, query: str) -> bool:
    if not query:
        return True
    values = [
        _row_id(row, view),
        row.get("content"),
        row.get("scope_name"),
        row.get("delivery"),
        row.get("guard_action"),
        row.get("retire_reason"),
        row.get("expires_at"),
    ]
    needle = query.casefold()
    return any(needle in str(value or "").casefold() for value in values)


def _filtered(rows: list[dict[str, Any]], view: str, query: str) -> list[dict[str, Any]]:
    return [row for row in rows if _matches(row, view, query)]


def _search(current: str) -> str | None:
    prompt = f"  search [{current}; empty clears]: " if current else "  search [empty clears]: "
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _confirm(question: str) -> bool:
    try:
        return input(f"  {question} [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _required(prompt: str) -> str | None:
    """Read a non-empty value; cancellation and emptiness both change nothing."""
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer or None


def _optional(prompt: str) -> str | None:
    try:
        return input(prompt).strip() or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _editor_text(content: str) -> str:
    """Edit text only through an explicitly configured editor."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        replacement = _required("  revised content [empty cancels]: ")
        if replacement is None:
            raise MashuError("revision cancelled")
        return replacement
    command = shlex.split(editor)
    if not command:
        raise MashuError("editor command is empty")
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
        revised = path.read_text(encoding="utf-8").strip()
        if not revised:
            raise MashuError("revision cannot be empty")
        return revised
    finally:
        path.unlink(missing_ok=True)


def _scope_id(cur: Any, name: str | None) -> UUID | None:
    return scopes.require_scope(cur, name)["scope_id"] if name else None


def _delivery_answers(
    default: str = "always",
    *,
    scope_default: str | None = None,
    action_default: str | None = None,
) -> tuple[str, str | None, str | None] | None:
    """Ask for a route and its route-specific fields before opening a transaction."""
    chosen = _optional(f"  delivery [always/scope/guard; enter={default}]: ") or default
    chosen = chosen.casefold()
    if chosen not in memories.DELIVERIES:
        raise MashuError("delivery must be always, scope, or guard")
    scope_name: str | None = None
    action: str | None = None
    if chosen == "scope":
        prompt = (
            f"  scope name [enter={scope_default}]: "
            if scope_default
            else "  scope name [required]: "
        )
        scope_name = _optional(prompt) or scope_default
        if scope_name is None:
            return None
    elif chosen == "guard":
        action_prompt = (
            f"  guard action [enter={action_default}]: "
            if action_default
            else "  guard action [required]: "
        )
        action = _optional(action_prompt) or action_default
        if action is None:
            return None
        scope_prompt = (
            f"  scope name [enter={scope_default}; '-' means every scope]: "
            if scope_default
            else "  scope name [empty means every scope]: "
        )
        entered_scope = _optional(scope_prompt)
        scope_name = scope_default if entered_scope is None else entered_scope
        if scope_name == "-":
            scope_name = None
    elif scope_default:
        scope_name = scope_default
    return chosen, scope_name, action


def _revise(dsn: str | None, row: dict[str, Any]) -> str:
    content = _editor_text(row["content"])
    with db.transaction(dsn) as cur:
        memories.revise(cur, row["memory_id"], content=content, actor=ACTOR)
    return screen.success(f"  ✓ revised {_short(row['memory_id'])}")


def _retire(dsn: str | None, row: dict[str, Any]) -> str:
    reason = _required("  retirement reason [required]: ")
    if reason is None:
        return screen.warning("  ! left active — retirement needs a reason")
    if not _confirm(f"retire {_short(row['memory_id'])}?"):
        return screen.warning("  ! left active — retirement was not confirmed")
    with db.transaction(dsn) as cur:
        memories.retire(cur, row["memory_id"], reason=reason, actor=ACTOR)
    return screen.success(f"  ✓ retired {_short(row['memory_id'])}")


def _set_delivery(dsn: str | None, row: dict[str, Any]) -> str:
    answers = _delivery_answers(
        row["delivery"],
        scope_default=row.get("scope_name"),
        action_default=row.get("guard_action"),
    )
    if answers is None:
        return screen.warning("  ! delivery unchanged")
    delivery, scope_name, action = answers
    with db.transaction(dsn) as cur:
        scope_id = _scope_id(cur, scope_name)
        changed = memories.set_delivery(
            cur,
            row["memory_id"],
            delivery=delivery,
            actor=ACTOR,
            guard_action=action,
            scope_id=scope_id,
            clear_scope=scope_id is None,
        )
    return screen.success(f"  ✓ delivery {_short(row['memory_id'])}  {changed['delivery']}")


def _conflict_text(conflict: RetiredConflictError) -> str:
    lines = [screen.danger(f"  {conflict}")]
    for row in conflict.tombstones:
        lines.append(
            screen.warning(f"  retired {_short(row['memory_id'])}  {_date(row.get('retired_at'))}")
        )
        lines.append(screen.wrap(f"reason: {row.get('retire_reason') or '-'}", indent="    "))
    return "\n".join(lines)


def _remember(dsn: str | None) -> tuple[str, UUID | None]:
    content = _required("  memory [empty cancels]: ")
    if content is None:
        return screen.warning("  ! nothing remembered"), None
    answers = _delivery_answers()
    if answers is None:
        return screen.warning("  ! nothing remembered"), None
    delivery, scope_name, action = answers
    override = False
    while True:
        try:
            with db.transaction(dsn) as cur:
                row = memories.remember(
                    cur,
                    content=content,
                    actor=ACTOR,
                    scope_id=_scope_id(cur, scope_name),
                    delivery=delivery,
                    guard_action=action,
                    override_retired=override,
                )
            return screen.success(f"  ✓ remembered {_short(row['memory_id'])}"), row["memory_id"]
        except RetiredConflictError as conflict:
            print(_conflict_text(conflict))
            if not _confirm("write it back despite the retirement reason?"):
                return screen.warning("  ! nothing remembered — retirement kept"), None
            override = True


def _temporary(dsn: str | None) -> tuple[str, UUID | None]:
    content = _required("  temporary context [empty cancels]: ")
    if content is None:
        return screen.warning("  ! nothing recorded"), None
    raw_days = _required("  days [more than 0, at most 14]: ")
    if raw_days is None:
        return screen.warning("  ! nothing recorded"), None
    try:
        days = float(raw_days)
    except ValueError as error:
        raise MashuError("days must be a number greater than 0 and at most 14") from error
    if not 0 < days <= 14:
        raise MashuError("days must be greater than 0 and at most 14")
    with db.transaction(dsn) as cur:
        row = temporary.put_temporary(
            cur,
            content=content,
            actor=ACTOR,
            days=days,
            scope_id=None,
        )
    return screen.success(f"  ✓ temporary {_short(row['context_id'])}"), row["context_id"]


def _move(at: int, total: int, key: str, page: int) -> int:
    if total <= 0:
        return 0
    if key in ("down", "j"):
        return min(total - 1, at + 1)
    if key in ("up", "k"):
        return max(0, at - 1)
    if key == "home":
        return 0
    if key == "end":
        return total - 1
    if key == "pagedown":
        return min(total - 1, at + max(1, page))
    if key == "pageup":
        return max(0, at - max(1, page))
    return at


@screen.fullscreen
def run(dsn: str | None = None) -> int:
    """Browse what Mashu remembers and carry out one explicit change at a time."""
    view = "active"
    query = ""
    at = {name: 0 for name in _VIEWS.values()}
    selected: dict[str, UUID | None] = {name: None for name in _VIEWS.values()}
    note = ""
    reading = False
    scroll = 0
    back: list[int] = []

    while True:
        try:
            all_rows = _rows(dsn, view)
        except MashuError as error:
            print(error)
            return 1
        rows = _filtered(all_rows, view, query)
        wanted = selected[view]
        kept = next(
            (number for number, row in enumerate(rows) if _row_id(row, view) == wanted), None
        )
        if kept is not None:
            at[view] = kept
        elif rows:
            at[view] = min(at[view], len(rows) - 1)
            selected[view] = _row_id(rows[at[view]], view)
        else:
            at[view] = 0

        if reading and rows:
            try:
                detail = (
                    _full_temporary(rows[at[view]])
                    if view == "temporary"
                    else _full_detail(_memory_record(dsn, _row_id(rows[at[view]], view)))
                )
            except MashuError as error:
                reading = False
                note = screen.danger(f"  ✗ {error}")
            else:
                under = screen.trailer(_ITEM_KEYS, note)
                page, more = screen.paged(detail, scroll, under)
                screen.paint(page + "\n" + under)
        if not reading:
            more = 0
            screen.paint(
                _screen_text(
                    rows,
                    at[view],
                    view,
                    note,
                    total=len(all_rows),
                    query=query,
                )
            )
        key = screen.getkey()
        note = ""

        if key == "q":
            return 0
        if reading:
            if key == "space" and more:
                back.append(scroll)
                scroll = more
                continue
            if key == "b":
                scroll = back.pop() if back else 0
                continue
            if key == "left":
                reading = False
                scroll, back = 0, []
            continue
        if key in _VIEWS:
            view = _VIEWS[key]
            query = ""
            continue
        if key == "/":
            searched = _search(query)
            if searched is not None:
                query = searched
                at[view] = 0
            continue
        if key == "left":
            if query:
                query = ""
                at[view] = 0
                continue
            return 0

        if key == "n":
            try:
                note, memory_id = _remember(dsn)
                if memory_id is not None:
                    view, query, selected["active"] = "active", "", memory_id
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
            continue
        if key == "p":
            try:
                note, context_id = _temporary(dsn)
                if context_id is not None:
                    view, query, selected["temporary"] = "temporary", "", context_id
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
            continue

        if not rows:
            continue
        row = rows[at[view]]
        if key in ("enter", "right", "l"):
            reading = True
            scroll, back = 0, []
            continue
        head = _heading(view, len(rows), len(all_rows), query)
        under = screen.trailer(_detail(row, view), _KEYS, note)
        page = screen.room_under(head, under)
        moved = _move(at[view], len(rows), key, page)
        if moved != at[view] or key in {
            "up",
            "down",
            "j",
            "k",
            "home",
            "end",
            "pageup",
            "pagedown",
        }:
            at[view] = moved
            selected[view] = _row_id(rows[moved], view)
            continue

        if view != "active" or key not in {"e", "r", "d"}:
            continue
        try:
            if key == "e":
                note = _revise(dsn, row)
            elif key == "r":
                note = _retire(dsn, row)
            else:
                note = _set_delivery(dsn, row)
        except MashuError as error:
            note = screen.danger(f"  ✗ {error}")
