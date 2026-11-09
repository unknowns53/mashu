"""Interactive browser and editor for durable and temporary memory."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from mashu import application, db, memories, memory_actions, screen, temporary, topics
from mashu.errors import MashuError, RetiredConflictError

ACTOR = "user"

_VIEWS = {"1": "active", "2": "retired", "3": "temporary"}
_KEYS = (
    "  ↑↓/jk move   PgUp/PgDn   / search   ⏎ why   ← back   q quit   ? help\n"
    "  n remember   p temporary   e edit   c convert   r retire/end   d delivery\n"
    "  o restore a retired memory   s sort   v reverse"
)
_ITEM_KEYS = "  space read on   b page back   ← back   q quit   ? help"

_HELP = """
  1 / 2 / 3    show active memories, retired memories, or temporary context
  ↑↓ or j/k    move one row
  Home/End     go to the first or last row
  PgUp/PgDn    move one screenful
  /            search by id, content, scope, delivery, topic, reason, or expiry
  ⏎ or →       read the selected row in full, with its evidence and revisions
  space        while reading, show the next screenful
  b            while reading, show the previous screenful
  n            remember a new rule
  p            add temporary context
  e            edit the selected memory or temporary context
  c            convert an active memory to temporary context, or the reverse
  r            retire the selected active memory, or end the selected temporary context
  d            change where the selected active memory is delivered
  o            restore the selected retired memory, after reading why it was retired
  s            sort by the next key: updated, created, delivery or kind, then back to
               the listed order (created, expires, scope for temporary context)
  v            reverse the order
  ← or Esc     clear the search; without a search, go back
  ?            show this help
  q            quit Mashu
"""
_HELP_KEYS = "  space read on   q quit   any other key goes back"

#: Each delivery in one plain line, shown wherever a person chooses one.
DELIVERY_CHOICES = (
    ("always", "every session, at start"),
    ("scope", "sessions in one Scope, at start"),
    ("topic", "its trigger is listed at start; read when that work begins"),
)


def delivery_legend() -> str:
    """The delivery choices, one plain line each."""
    return "\n".join(f"    {name:<6}  {meaning}" for name, meaning in DELIVERY_CHOICES)


def _short(value: Any) -> str:
    return str(value)[:8]


def _date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    return str(value or "-")


def _row_id(row: dict[str, Any], view: str) -> UUID:
    return row["context_id" if view == "temporary" else "memory_id"]


def _memory_record(dsn: str | None, memory_id: UUID) -> dict[str, Any]:
    """Read a memory together with the evidence and wording history behind it."""
    with db.transaction(dsn) as cur:
        row = memories.memory_details(cur, memory_id)
    if row is None:
        raise MashuError(f"no memory {memory_id}")
    return row


def _delivery(row: dict[str, Any]) -> str:
    delivery = row["delivery"]
    if delivery == "topic":
        return f"topic:{row.get('topic_name') or '-'}"
    return delivery


def _columns(rows: list[dict[str, Any]], width: int) -> tuple[int, int]:
    """Delivery and scope widths that fit the longest value listed.

    Capped so one long topic or scope name cannot take the content's room.
    """
    delivery = max((screen.cells(_delivery(row)) for row in rows if "delivery" in row), default=1)
    scope = max((screen.cells(row.get("scope_name") or "-") for row in rows), default=1)
    return min(delivery, max(6, width // 6)), min(scope, max(6, width // 7))


def _line(row: dict[str, Any], view: str, width: int, columns: tuple[int, int]) -> str:
    delivery, scope = columns
    scope_name = screen.pad(row.get("scope_name") or "-", scope)
    if view == "temporary":
        fixed = f"   {_short(row['context_id'])}  {scope_name}  "
        expiry = _date(row["expires_at"])
        return screen.clip(f"{fixed}{expiry}  {row['content']}", width)
    route = screen.pad(_delivery(row), delivery)
    fixed = f"   {_short(row['memory_id'])}  {route}  {scope_name}  "
    return screen.clip(f"{fixed}{row['content']}", width)


def _field(label: str, value: Any) -> str:
    prefix = f"  {label:<10}"
    text = " ".join(str(value or "-").splitlines())
    return f"{screen.bold(prefix)}{screen.clip(text, max(1, screen.terminal_width() - 12))}"


def _detail(row: dict[str, Any], view: str) -> str:
    """The selected row's complete decision fields, cut to the room below the list."""
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
            _field("delivery", _delivery(row)),
            _field("scope", row.get("scope_name") or "-"),
        ]
        if row.get("delivery") == "topic":
            lines.append(_field("trigger", row.get("topic_trigger") or "-"))
        if view == "retired":
            lines.append(_field("kind", row.get("retirement_kind") or "legacy"))
            lines.append(_field("retired", _date(row.get("retired_at"))))
            lines.append(_field("reason", row.get("retire_reason") or "-"))
            if row.get("superseded_by"):
                lines.append(_field("successor", row["superseded_by"]))
            if row.get("relocated_to_id"):
                lines.append(
                    _field(
                        "moved to",
                        f"{row['relocated_to_kind']} {row['relocated_to_id']}",
                    )
                )
        lines.extend(
            [
                screen.bold("  content"),
                screen.wrap(row.get("content") or "", indent="    "),
            ]
        )

    return "\n".join(lines)


def _full_detail(row: dict[str, Any]) -> str:
    """The complete account of what this rule says and why it exists."""
    status = row["status"]
    lines = [
        screen.bold(f"  memory {_short(row['memory_id'])}  {status}"),
        screen.dim(f"  {_date(row.get('created_at'))}  by {row.get('created_by') or '-'}"),
        "",
        _field("delivery", _delivery(row)),
        _field("scope", row.get("scope_name") or "-"),
    ]
    if row.get("delivery") == "topic":
        lines.extend(
            [
                screen.bold("  trigger"),
                screen.wrap(row.get("topic_trigger") or "-", indent="    "),
            ]
        )
    if status == "retired":
        lines.extend(
            [
                _field("retired", _date(row.get("retired_at"))),
                _field("kind", row.get("retirement_kind") or "legacy"),
                screen.bold("  retirement reason"),
                screen.wrap(row.get("retire_reason") or "-", indent="    "),
            ]
        )
        if row.get("superseded_by"):
            lines.append(_field("successor", row["superseded_by"]))
        if row.get("relocated_to_id"):
            lines.append(_field("moved to", f"{row['relocated_to_kind']} {row['relocated_to_id']}"))
    lines.extend([screen.bold("  content"), screen.wrap(row["content"], indent="    "), ""])

    lines.append(screen.accent("  evidence"))
    evidence = row.get("basis") or []
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
    revisions = row.get("revisions") or []
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


def _tabs(view: str) -> str:
    tabs = []
    for key, name in _VIEWS.items():
        label = f"{key} {name}"
        tabs.append(screen.bold(f"[{label}]") if name == view else label)
    return "  " + "   ".join(tabs)


Order = tuple[str, bool] | None


def _order_text(order: Order) -> str:
    if order is None:
        return ""
    key, descending = order
    return f"  ·  sorted by {key} {'↓' if descending else '↑'}"


def _ordered(rows: list[dict[str, Any]], order: Order) -> list[dict[str, Any]]:
    """Rows in the order chosen with s and v; None keeps the order the store lists them in."""
    if order is None:
        return rows
    key, descending = order
    return application.sort_rows(rows, key, descending=descending)


def _heading(view: str, shown: int, total: int, query: str, order: Order = None) -> str:
    name = "temporary context" if view == "temporary" else f"{view} memories"
    count = f"{shown}/{total}" if query else str(total)
    suffix = f"  ·  search {screen.accent(repr(query))}" if query else ""
    suffix += _order_text(order)
    title = screen.bold(screen.accent("Memories"))
    return f"{title}\n{_tabs(view)}\n\n  {screen.bold(name)}  {count}{suffix}"


def _help() -> None:
    offset = 0
    body = _HELP.strip("\n")
    while True:
        title = screen.bold(screen.accent("Memories help"))
        page, more = screen.paged(f"{title}\n\n\n{body}", offset, _HELP_KEYS)
        screen.paint(page + "\n" + _HELP_KEYS)
        key = screen.getkey()
        if key == "q":
            raise screen.Quit()
        if key != "space" or not more:
            return
        offset = more


def _screen_text(
    rows: list[dict[str, Any]],
    at: int,
    view: str,
    note: str,
    *,
    total: int,
    query: str,
    order: Order = None,
) -> str:
    head = _heading(view, len(rows), total, query, order)
    width = screen.terminal_width() - 1
    columns = _columns(rows, width)
    rendered = [_line(row, view, width, columns) for row in rows]
    if rows:
        rendered[at] = screen.selected(rendered[at])
    if not rows:
        rendered.append(screen.dim("  no rows match; / searches again, ← clears the search"))
    detail = _detail(rows[at], view) if rows else ""
    return screen.list_with_preview(head, rendered, at, detail, _KEYS, note)


def _matches(row: dict[str, Any], view: str, query: str) -> bool:
    if not query:
        return True
    values = [
        _row_id(row, view),
        row.get("content"),
        row.get("scope_name"),
        row.get("delivery"),
        row.get("topic_name"),
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


def _topic_line(number: int, row: dict[str, Any]) -> str:
    scope = row.get("scope_name") or "every session"
    rules = f"{row['rules']} rule" + ("" if row["rules"] == 1 else "s")
    return screen.clip(
        f"    {number:>2}  {screen.pad(row['name'], 20)} {screen.pad(scope, 14)} "
        f"{rules:>8}  {row['trigger']}",
        screen.terminal_width() - 1,
    )


def pick_topic(dsn: str | None, current: Any = None) -> dict[str, Any] | None:
    """Choose an open topic by number, or describe a new one; nothing is written here."""
    with db.transaction(dsn) as cur:
        rows = topics.list_topics(cur)
    print(screen.bold("  topics"))
    for number, row in enumerate(rows, 1):
        print(_topic_line(number, row))
    print("     n  a new topic")
    default = next(
        (str(number) for number, row in enumerate(rows, 1) if row["topic_id"] == current), ""
    )
    hint = f"; enter={default}" if default else ""
    answer = screen.editline(f"  topic [number, or n for a new one{hint}]: ", default)
    if isinstance(answer, screen.Cancelled):
        return None
    chosen = (answer.text or default).strip().casefold()
    if not chosen:
        return None
    if chosen in ("n", "new"):
        return _new_topic()
    if chosen.isdigit() and 1 <= int(chosen) <= len(rows):
        row = rows[int(chosen) - 1]
        return {"topic_id": row["topic_id"], "name": row["name"]}
    raise MashuError(f"choose a topic number from 1 to {len(rows)}, or n for a new topic")


def _new_topic() -> dict[str, Any] | None:
    """Ask for the three things a topic is: its name, where it is listed, and its trigger."""
    name = _required(f"  new topic name [at most {topics.NAME_LIMIT} characters]: ")
    if name is None:
        return None
    scope = screen.editline("  scope [empty means every session]: ", "")
    if isinstance(scope, screen.Cancelled):
        return None
    print(
        screen.dim(
            "  the trigger says when to read it, e.g. 'Before changing difficulty levers or "
            "win rates'"
        )
    )
    trigger = _required(f"  trigger [one sentence, at most {topics.TRIGGER_LIMIT} characters]: ")
    if trigger is None:
        return None
    return {"topic_id": None, "name": name, "scope_name": scope.text or None, "trigger": trigger}


def _chosen_delivery(text: str) -> str:
    """A delivery by name, or by its first letter."""
    chosen = text.strip().casefold()
    for name, _meaning in DELIVERY_CHOICES:
        if chosen in (name, name[0]):
            return name
    raise MashuError("delivery must be always, scope, or topic")


def _delivery_answers(
    default: str = "always",
    *,
    scope_default: str | None = None,
    topic_default: Any = None,
    dsn: str | None = None,
) -> tuple[str, str | None, dict[str, Any] | None] | None:
    """Ask for a route and its route-specific fields before opening a transaction."""
    print(screen.bold("  where should this rule be delivered?"))
    print(delivery_legend())
    chosen_answer = screen.editline("  delivery [always/scope/topic; edit existing]: ", default)
    if isinstance(chosen_answer, screen.Cancelled):
        return None
    chosen = _chosen_delivery(chosen_answer.text or default)
    scope_name: str | None = None
    topic: dict[str, Any] | None = None
    if chosen == "topic":
        topic = pick_topic(dsn, topic_default)
        if topic is None:
            return None
    elif chosen == "scope":
        prompt = (
            f"  scope name [enter={scope_default}]: "
            if scope_default
            else "  scope name [required]: "
        )
        entered_scope = screen.editline(prompt, scope_default or "")
        if isinstance(entered_scope, screen.Cancelled):
            return None
        scope_name = entered_scope.text or scope_default
        if scope_name is None:
            return None
    elif scope_default:
        # An always rule may keep the scope it belonged to; '-' drops it, as mashu deliver
        # --no-scope does.
        kept_scope = screen.editline(
            f"  scope it belongs to [enter={scope_default}, - drops it]: ", scope_default
        )
        if isinstance(kept_scope, screen.Cancelled):
            return None
        kept = kept_scope.text or scope_default
        scope_name = None if kept == "-" else kept
    return chosen, scope_name, topic


def _revise(dsn: str | None, row: dict[str, Any]) -> str:
    content = screen.edit_long_text("Edit memory", row["content"])
    acknowledged: list[UUID] | None = None
    while True:
        try:
            with db.transaction(dsn) as cur:
                memories.revise(
                    cur,
                    row["memory_id"],
                    content=content,
                    actor=ACTOR,
                    acknowledged_conflicts=acknowledged,
                )
            return screen.success(f"  ✓ revised {_short(row['memory_id'])}")
        except RetiredConflictError as conflict:
            print(_conflict_text(conflict))
            if not _confirm("rewrite it after reading these retirement reasons?"):
                return screen.warning("  ! nothing revised — retirement kept")
            acknowledged = [tomb["memory_id"] for tomb in conflict.tombstones]


def _revise_temporary(dsn: str | None, row: dict[str, Any]) -> str:
    content = screen.edit_long_text("Edit temporary context", row["content"])
    current_scope = row.get("scope_name")
    scope_prompt = (
        f"  scope name [enter={current_scope}; '-' means every scope]: "
        if current_scope
        else "  scope name [empty means every scope]: "
    )
    entered_scope = screen.editline(scope_prompt, current_scope or "")
    if isinstance(entered_scope, screen.Cancelled):
        return screen.warning("  ! temporary context unchanged")
    scope_name = current_scope if not entered_scope.text else entered_scope.text
    if scope_name == "-":
        scope_name = None
    with db.transaction(dsn) as cur:
        memory_actions.revise_temporary(
            cur, row["context_id"], content=content, scope_name=scope_name, actor=ACTOR
        )
    return screen.success(f"  ✓ revised temporary {_short(row['context_id'])}")


def _retire(dsn: str | None, row: dict[str, Any]) -> str:
    choices = memory_actions.RETIREMENT_KINDS
    answer = screen.editline(f"  retirement kind [{'/'.join(choices)}]: ", "")
    if isinstance(answer, screen.Cancelled):
        return screen.warning("  ! retirement cancelled; Memory remains active")
    retirement_kind = answer.text
    if retirement_kind not in choices:
        return screen.warning("  ! left active — choose a supported retirement kind")
    successor_ref = None
    context_ref = None
    if retirement_kind == "superseded":
        successor_ref = _required("  successor Memory ID [required]: ")
        if successor_ref is None:
            return screen.warning("  ! left active — successor Memory is required")
    if retirement_kind == "relocated":
        context_ref = _required("  Temporary Context ID [required]: ")
        if context_ref is None:
            return screen.warning("  ! left active — destination Temporary Context is required")
    reason = _required("  retirement reason [required]: ")
    if reason is None:
        return screen.warning("  ! left active — retirement needs a reason")
    try:
        with db.transaction(dsn) as cur:
            memory_actions.retire(
                cur,
                row["memory_id"],
                kind=retirement_kind,
                reason=reason,
                successor=successor_ref,
                destination=context_ref,
                actor=ACTOR,
            )
    except MashuError as refusal:
        return screen.danger(f"  {refusal}")
    return screen.success(f"  ✓ retired {_short(row['memory_id'])}")


def _restore(dsn: str | None, row: dict[str, Any]) -> tuple[str, bool]:
    """Restore a retired rule; the note to show, and whether it is active again."""
    kind = row.get("retirement_kind") or "legacy"
    print(screen.wrap(f"  retired as {kind}: {row.get('retire_reason') or '-'}", indent="    "))
    reason = _required("  why the retirement no longer holds [required]: ")
    if reason is None:
        return screen.warning("  ! left retired — restoring needs a reason"), False
    acknowledged: list[UUID] | None = None
    while True:
        try:
            with db.transaction(dsn) as cur:
                memory_actions.restore(
                    cur, row["memory_id"], reason=reason, acknowledged=acknowledged, actor=ACTOR
                )
            return screen.success(f"  ✓ restored {_short(row['memory_id'])}"), True
        except RetiredConflictError as conflict:
            print(_conflict_text(conflict))
            if not _confirm("restore it after reading these retirement reasons?"):
                return screen.warning("  ! left retired"), False
            acknowledged = [tomb["memory_id"] for tomb in conflict.tombstones]
        except MashuError as refusal:
            return screen.danger(f"  {refusal}"), False


def _set_delivery(dsn: str | None, row: dict[str, Any]) -> str:
    answers = _delivery_answers(
        row["delivery"],
        scope_default=row.get("scope_name"),
        topic_default=row.get("topic_id"),
        dsn=dsn,
    )
    if answers is None:
        return screen.warning("  ! delivery unchanged")
    delivery, scope_name, topic = answers
    with db.transaction(dsn) as cur:
        changed = memory_actions.set_delivery(
            cur,
            row["memory_id"],
            delivery=delivery,
            scope_name=scope_name,
            topic=topic,
            actor=ACTOR,
        )
    label = f"topic:{topic['name']}" if topic else changed["delivery"]
    return screen.success(f"  ✓ delivery {_short(row['memory_id'])}  {label}")


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
    why = _optional("  why [optional: reason, measurements, dates, history]: ")
    # Refused before the delivery questions, so an over-long body costs no further answers.
    memories.check_body(content, why=why)
    answers = _delivery_answers(dsn=dsn)
    if answers is None:
        return screen.warning("  ! nothing remembered"), None
    delivery, scope_name, topic = answers
    acknowledged: list[UUID] | None = None
    while True:
        try:
            with db.transaction(dsn) as cur:
                row = memory_actions.remember(
                    cur,
                    content=content,
                    why=why,
                    delivery=delivery,
                    scope_name=scope_name,
                    topic=topic,
                    acknowledged=acknowledged,
                    actor=ACTOR,
                )
            message = screen.success(f"  ✓ remembered {_short(row['memory_id'])}")
            for warning in row.get("retirement_warnings", []):
                message += "\n" + screen.warning(
                    f"  ! {warning.get('retirement_kind') or 'legacy'} retirement "
                    f"{_short(warning['memory_id'])}: {warning['retire_reason']}"
                )
                if warning.get("superseded_by"):
                    message += "\n" + screen.wrap(
                        f"successor: {warning['superseded_by']}", indent="    "
                    )
                if warning.get("relocated_to_id"):
                    message += "\n" + screen.wrap(
                        f"moved to {warning['relocated_to_kind']}: {warning['relocated_to_id']}",
                        indent="    ",
                    )
            return message, row["memory_id"]
        except RetiredConflictError as conflict:
            print(_conflict_text(conflict))
            if not _confirm("write it back after reading these retirement reasons?"):
                return screen.warning("  ! nothing remembered — retirement kept"), None
            acknowledged = [row["memory_id"] for row in conflict.tombstones]


def _days() -> float | None:
    raw_days = _required("  days [more than 0, at most 14; empty cancels]: ")
    if raw_days is None:
        return None
    try:
        days = float(raw_days)
    except ValueError as error:
        raise MashuError("days must be a number greater than 0 and at most 14") from error
    if not 0 < days <= 14:
        raise MashuError("days must be greater than 0 and at most 14")
    return days


def _temporary(dsn: str | None) -> tuple[str, UUID | None]:
    content = _required("  temporary context [empty cancels]: ")
    if content is None:
        return screen.warning("  ! nothing recorded"), None
    days = _days()
    if days is None:
        return screen.warning("  ! nothing recorded"), None
    with db.transaction(dsn) as cur:
        row = memory_actions.add_temporary(cur, content=content, days=days, actor=ACTOR)
    return screen.success(f"  ✓ temporary {_short(row['context_id'])}"), row["context_id"]


def _convert_to_temporary(dsn: str | None, row: dict[str, Any]) -> tuple[str, UUID | None]:
    if row["delivery"] == "topic":
        return screen.warning("  ! topic memories cannot become temporary"), None
    days = _days()
    if days is None:
        return screen.warning("  ! conversion cancelled"), None
    with db.transaction(dsn) as cur:
        converted = memories.convert_to_temporary(
            cur,
            row["memory_id"],
            days=days,
            actor=ACTOR,
        )
    context = converted["temporary"]
    note = screen.success(
        f"  ✓ converted {_short(row['memory_id'])} to temporary {_short(context['context_id'])}"
    )
    return note, context["context_id"]


def _end_temporary(dsn: str | None, row: dict[str, Any]) -> str:
    if not _confirm(f"end temporary {_short(row['context_id'])} now?"):
        return screen.warning("  ! temporary context unchanged")
    with db.transaction(dsn) as cur:
        temporary.end(cur, row["context_id"], actor=ACTOR)
    return screen.success(f"  ✓ ended temporary {_short(row['context_id'])}")


def _convert_to_memory(dsn: str | None, row: dict[str, Any]) -> tuple[str, UUID | None]:
    if not _confirm(f"convert temporary {_short(row['context_id'])} to active memory?"):
        return screen.warning("  ! conversion cancelled"), None
    acknowledged: list[UUID] | None = None
    while True:
        try:
            with db.transaction(dsn) as cur:
                converted = temporary.convert_to_memory(
                    cur, row["context_id"], actor=ACTOR, acknowledged_conflicts=acknowledged
                )
            break
        except RetiredConflictError as conflict:
            print(_conflict_text(conflict))
            if not _confirm("convert it after reading these retirement reasons?"):
                return screen.warning("  ! nothing converted — retirement kept"), None
            acknowledged = [tomb["memory_id"] for tomb in conflict.tombstones]
    memory = converted["memory"]
    note = screen.success(
        f"  ✓ converted {_short(row['context_id'])} to memory {_short(memory['memory_id'])}"
    )
    return note, memory["memory_id"]


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


def _next_order(current: Order, keys: tuple[str, ...], key: str) -> Order:
    """s moves to the next key in its own default direction, and from the last back to the
    listed order; v reverses the current key, starting from the first when none is chosen."""
    if key == "v":
        name, descending = current or (keys[0], keys[0] in application.DATE_SORTS)
        return name, not descending
    if current is None:
        return keys[0], keys[0] in application.DATE_SORTS
    at = keys.index(current[0]) + 1
    if at == len(keys):
        return None
    return keys[at], keys[at] in application.DATE_SORTS


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
    order: dict[str, Order] = {name: None for name in _VIEWS.values()}

    while True:
        try:
            with db.transaction(dsn) as cur:
                all_rows = application.memory_view(cur, view)
        except MashuError as error:
            print(error)
            return 1
        rows = _ordered(_filtered(all_rows, view, query), order[view])
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
                    order=order[view],
                )
            )
        key = screen.getkey()
        note = ""

        if key == "q":
            raise screen.Quit()
        if key == "?":
            _help()
            continue
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
        if key in ("s", "v"):
            order[view] = _next_order(order[view], application.MEMORY_SORTS[view], key)
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
        moved = _move(at[view], len(rows), key, screen.list_room(head, _KEYS, len(rows)))
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

        if key == "c":
            try:
                if view == "active":
                    note, converted_id = _convert_to_temporary(dsn, row)
                    if converted_id is not None:
                        view, query, selected["temporary"] = "temporary", "", converted_id
                elif view == "temporary":
                    note, converted_id = _convert_to_memory(dsn, row)
                    if converted_id is not None:
                        view, query, selected["active"] = "active", "", converted_id
                else:
                    note = screen.warning("  ! retired memories cannot be converted")
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
            continue
        if view == "retired" and key == "o":
            note, restored = _restore(dsn, row)
            if restored:
                view, query, selected["active"] = "active", "", row["memory_id"]
            continue
        if view == "temporary" and key in {"e", "r"}:
            try:
                note = _revise_temporary(dsn, row) if key == "e" else _end_temporary(dsn, row)
            except MashuError as error:
                note = screen.danger(f"  ✗ {error}")
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
