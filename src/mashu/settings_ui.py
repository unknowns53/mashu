"""Settings and store-health screens for people running Mashu interactively."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mashu import (
    application,
    config,
    db,
    routing,
    scopes,
    screen,
    topics,
)
from mashu import migrate as migration
from mashu.errors import MashuError

ACTOR = "user"

_TOP_KEYS = "  ↑↓/jk move   ⏎ open   ? help   q leave"
_LIST_KEYS = "  ↑↓/jk move   Home/End first/last   ←/q settings   ? help"
_READ_KEYS = "  space/PgDn read on   b/PgUp back   Home top   ←/q settings   ? help"

_TOP_ITEMS = (
    ("Health/status", "capacity, queues, recent evidence, and work"),
    ("Bootstrap preview", "what a session opened here receives"),
    ("Scopes", "memory homes and their opening cost"),
    ("Topics", "rules read when one kind of work begins"),
    ("Routes", "directory trees mapped to scopes or ignored"),
    ("Schema/migrations", "database version and pending changes"),
)

_HELP = """
  Settings & health is the human view of Mashu's operating state.

  ↑↓ or j/k  move through a list
  ⏎            open the selected page
  space        read the next screenful of a long page
  b            go back one screenful
  Home/End     jump to the first or last list item
  ← or q       return one level; from this page, leave

  Scope, topic, and route pages show their own write keys. A topic is archived
  only once it holds no active rule. Route removal, topic archiving, and schema
  migration always ask for confirmation before changing the store.
"""


@dataclass(frozen=True)
class Health:
    schema_pending: tuple[str, ...]
    memory_counts: dict[str, int]
    memory_always_tokens: int
    memory_worst_tokens: int
    active_states: int
    state_worst_tokens: int
    temporary_count: int
    temporary_tokens: int
    pending_ready: int
    pending_deferred: int
    traces: int
    ledger_30d: int
    delivery_failures_30d: int
    delivery_failures_by_route: dict[str, int]
    scope_count: int
    topic_count: int
    topic_heaviest_name: str | None
    topic_heaviest_tokens: int
    tasks_active: int
    tasks_dormant: int
    tasks_closed: int


def _health(dsn: str | None) -> Health:
    """Collect the status page in one consistent database snapshot."""
    with db.transaction(dsn) as cur:
        snapshot = application.status_snapshot(cur)

    return Health(
        schema_pending=snapshot.schema_pending,
        memory_counts=snapshot.memory_counts,
        memory_always_tokens=snapshot.memory_always_tokens,
        memory_worst_tokens=snapshot.memory_worst_tokens,
        active_states=snapshot.active_states,
        state_worst_tokens=snapshot.state_worst_tokens,
        temporary_count=snapshot.temporary_count,
        temporary_tokens=snapshot.temporary_tokens,
        pending_ready=snapshot.pending_ready,
        pending_deferred=snapshot.pending_deferred,
        traces=snapshot.traces,
        ledger_30d=snapshot.ledger_30d,
        delivery_failures_30d=snapshot.delivery_failures_30d,
        delivery_failures_by_route=snapshot.delivery_failures_by_route,
        scope_count=snapshot.scope_count,
        topic_count=snapshot.topic_count,
        topic_heaviest_name=snapshot.topic_heaviest_name,
        topic_heaviest_tokens=snapshot.topic_heaviest_tokens,
        tasks_active=snapshot.tasks_active,
        tasks_dormant=snapshot.tasks_dormant,
        tasks_closed=snapshot.tasks_closed,
    )


def _routes(by_route: dict[str, int]) -> str:
    """The non-zero routes of the suspected failures, in status order."""
    shown = [
        f"{route} {by_route[route]}"
        for route in application.DELIVERY_FAILURE_ROUTES
        if by_route.get(route)
    ]
    return f"  ({', '.join(shown)})" if shown else ""


def _health_text(value: Health) -> str:
    if value.schema_pending:
        schema = screen.warning(
            f"{len(value.schema_pending)} pending: {', '.join(value.schema_pending)}"
        )
    else:
        schema = screen.success("up to date")
    deliveries = "  ·  ".join(
        f"{name} {value.memory_counts.get(name, 0)}" for name in ("always", "scope", "topic")
    )
    heaviest = (
        f"  ·  heaviest {value.topic_heaviest_name} "
        f"{value.topic_heaviest_tokens}/{config.topic_capacity()} tokens"
        if value.topic_heaviest_name
        else ""
    )
    return "\n".join(
        (
            f"  Schema             {schema}",
            "",
            f"  Active memories    {deliveries}",
            f"  Memory capacity    always {value.memory_always_tokens}/{config.always_capacity()}"
            f"  ·  worst {value.memory_worst_tokens}/{config.capacity()}",
            f"  Task cards         {value.active_states} active"
            f"  ·  worst {value.state_worst_tokens}/{config.project_capacity()} tokens",
            f"  Temporary          {value.temporary_count} standing"
            f"  ·  {value.temporary_tokens}/{config.temporary_capacity()} tokens",
            "",
            f"  Review queue       {value.pending_ready} ready"
            f"  ·  {value.pending_deferred} deferred",
            f"  Evidence           {value.traces} live traces"
            f"  ·  {value.ledger_30d} ledger entries in 30 days",
            f"  Delivery health    {value.delivery_failures_30d} suspected failures in 30 days"
            + _routes(value.delivery_failures_by_route),
            f"  Scopes             {value.scope_count}",
            f"  Topics             {value.topic_count}{heaviest}",
            f"  Tasks              {value.tasks_active} active"
            f"  ·  {value.tasks_dormant} dormant  ·  {value.tasks_closed} closed",
        )
    )


def _bootstrap_preview(dsn: str | None) -> dict[str, Any]:
    """Build the same payload a session started in the current directory receives."""
    with db.transaction(dsn) as cur:
        return application.bootstrap_preview(cur, cwd=os.getcwd(), actor=ACTOR)


def _memory_lines(label: str, rows: list[dict[str, Any]]) -> list[str]:
    lines = [f"  {label} ({len(rows)})"]
    if not rows:
        lines.append("    —")
    for row in rows:
        memory_id = str(row.get("memory_id", ""))[:8]
        lines.append(screen.wrap(f"{memory_id}  {row['content']}", indent="    "))
    return lines


def _bootstrap_text(answer: dict[str, Any]) -> str:
    routed = "yes" if answer.get("routed") else "no"
    lines = [f"  Scope              {answer.get('scope') or '—'}  ·  routed {routed}", ""]
    pending = answer.get("schema_pending") or []
    if pending:
        lines.extend([f"  Schema             {len(pending)} migration(s) pending", ""])
    lines.extend(_memory_lines("Always memories", answer.get("always") or []))
    lines.extend(["", *_memory_lines("Scoped memories", answer.get("scoped") or []), ""])
    index = answer.get("topics") or []
    lines.append(f"  Topics ({len(index)})")
    if not index:
        lines.append("    —")
    for row in index:
        lines.append(screen.wrap(row["line"], indent="    "))
    if answer.get("topic_instruction"):
        lines.append(screen.wrap(answer["topic_instruction"], indent="    "))
    lines.append("")

    states = answer.get("states") or []
    lines.append(f"  Active task cards ({len(states)})")
    if not states:
        lines.append("    —")
    for row in states:
        lines.append(f"    {row['task']}  {row['heading']}")
        lines.append(screen.wrap(row["content"], indent="      "))
    if answer.get("task_instruction"):
        lines.append(screen.wrap(answer["task_instruction"], indent="    "))

    contexts = answer.get("temporary") or []
    lines.extend(["", f"  Temporary context ({len(contexts)})"])
    if not contexts:
        lines.append("    —")
    for row in contexts:
        expires = row.get("expires_at")
        expires_text = expires.isoformat() if hasattr(expires, "isoformat") else str(expires)
        lines.append(screen.wrap(f"until {expires_text}  {row['content']}", indent="    "))

    over = "  ·  OVER CAPACITY" if answer.get("over_budget") else ""
    lines.extend(
        (
            "",
            f"  Review candidates  {answer.get('pending', 0)} pending",
            f"  Tokens             memory {answer.get('memory_tokens', 0)}"
            f"  ·  cards {answer.get('card_tokens', answer.get('project_tokens', 0))}"
            f"  ·  temporary {answer.get('temporary_tokens', 0)}",
            f"  Total              {answer.get('tokens', 0)}/{answer.get('capacity', 0)}{over}",
        )
    )
    return "\n".join(lines)


def _title(text: str) -> str:
    return screen.bold(screen.accent(text))


def _read_page(title: str, body: str, *, allow_help: bool = True) -> None:
    """Read a potentially long page, keeping a back-stack of screenfuls."""
    offset = 0
    back: list[int] = []
    while True:
        page, more = screen.paged(f"{_title(title)}\n\n\n{body}", offset, _READ_KEYS)
        screen.paint(f"{page}\n{_READ_KEYS}")
        key = screen.getkey()
        if key in ("q", "left"):
            return
        if key == "?" and allow_help:
            _help()
            continue
        if key == "home":
            offset, back = 0, []
            continue
        if key in ("pageup", "b", "up"):
            offset = back.pop() if back else 0
            continue
        if key in ("space", "pagedown", "down") and more:
            back.append(offset)
            offset = more


def _help() -> None:
    _read_page("Settings & health help", _HELP.strip(), allow_help=False)


def _answer(prompt: str) -> screen.InputResult:
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return screen.Cancelled()
    return screen.Submitted(value)


def _confirm(question: str) -> bool:
    try:
        return input(f"  {question} [y/N]: ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _replacement(
    prompt: str, current: str | None, *, clearable: bool = True
) -> str | None | screen.Cancelled:
    clearing = "; '-' clears" if clearable else ""
    answer = screen.editline(f"  {prompt} [edit existing{clearing}]: ", current or "")
    if isinstance(answer, screen.Cancelled):
        return answer
    if not answer.text:
        return current
    if clearable and answer.text == "-":
        return None
    return answer.text


def _scope_rows(dsn: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return scopes.list_scopes(cur)


def _scope_detail(row: dict[str, Any]) -> str:
    summary = row.get("summary") or "No summary written."
    return "\n".join(
        (
            f"  ── {row['name']}",
            screen.wrap(summary, indent="  "),
            f"  {row['n_active']} active memories  ·  {row['push_tokens']} pushed tokens",
        )
    )


def _scopes_page(dsn: str | None) -> None:
    at, note, selected = 0, "", None
    while True:
        rows = _scope_rows(dsn)
        kept = next((index for index, row in enumerate(rows) if row["scope_id"] == selected), None)
        if kept is not None:
            at = kept
        selected = None
        at = max(0, min(at, len(rows) - 1))
        lines = []
        for index, row in enumerate(rows):
            line = screen.clip(
                f"    {screen.pad(row['name'], 24)} {row['n_active']:>3} memories"
                f"  ·  {row['push_tokens']:>4} tokens",
                screen.terminal_width(),
            )
            lines.append(screen.selected(line) if index == at else line)
        detail = _scope_detail(rows[at]) if rows else "  No scopes yet. Press n to create one."
        keys = screen.trailer(
            detail,
            "  n new scope   e edit   " + _LIST_KEYS.strip(),
            note,
        )
        screen.paint(screen.list_screen(_title("Scopes"), lines, at, keys))
        key = screen.getkey()
        note = ""
        if key in ("q", "left"):
            return
        if key == "?":
            _help()
        elif key in ("up", "k"):
            at = max(0, at - 1)
        elif key in ("down", "j"):
            at = min(max(0, len(rows) - 1), at + 1)
        elif key == "home":
            at = 0
        elif key == "end":
            at = max(0, len(rows) - 1)
        elif key == "e":
            if not rows:
                note = "  there is no scope to edit"
                continue
            row = rows[at]
            selected = row["scope_id"]
            name = _replacement("scope name", row["name"], clearable=False)
            summary = _replacement("about", row.get("summary"))
            if isinstance(name, screen.Cancelled) or isinstance(summary, screen.Cancelled):
                note = "  scope unchanged"
                continue
            try:
                with db.transaction(dsn) as cur:
                    scopes.update_scope(
                        cur,
                        row["scope_id"],
                        name=name or row["name"],
                        summary=summary,
                        actor=ACTOR,
                    )
                note = f"  edited scope {name or row['name']}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "n":
            name_answer = _answer("  scope name: ")
            if isinstance(name_answer, screen.Cancelled):
                note = "  scope creation cancelled"
                continue
            if not name_answer.text:
                note = "  a scope needs a name"
                continue
            summary_answer = _answer("  about [optional]: ")
            if isinstance(summary_answer, screen.Cancelled):
                note = "  scope creation cancelled"
                continue
            try:
                with db.transaction(dsn) as cur:
                    created = scopes.create_scope(
                        cur,
                        name=name_answer.text,
                        summary=summary_answer.text or None,
                        actor=ACTOR,
                    )
                note = f"  created scope {name_answer.text}"
                if created.get("unchecked"):
                    note += "; banned-pattern list unavailable, so nothing was checked"
                elif created.get("malformed"):
                    note += "; some banned-pattern lines could not be compiled"
            except MashuError as error:
                note = f"  {error}"


def _topic_rows(dsn: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return topics.list_topics(cur)


def _rules(count: int) -> str:
    return f"{count} rule" if count == 1 else f"{count} rules"


def _topic_detail(row: dict[str, Any]) -> str:
    where = f"scope {row['scope_name']}" if row.get("scope_name") else "every session"
    before = f"  ·  before: {row['action']}" if row.get("action") else ""
    return "\n".join(
        (
            f"  ── {row['name']}  ·  listed in {where}{before}",
            screen.wrap(f"trigger: {row['trigger']}", indent="  "),
            f"  {_rules(row['rules'])} active  ·  body {row['body_tokens']}/"
            f"{config.topic_capacity()} tokens  ·  index line {row['line_tokens']} tokens",
        )
    )


def _topic_scope(prompt: str, current: str | None) -> str | None | screen.Cancelled:
    """A scope name, or None for every session; '-' clears one already set."""
    hint = "; '-' means every session" if current else "; empty means every session"
    answer = screen.editline(f"  {prompt} [edit existing{hint}]: ", current or "")
    if isinstance(answer, screen.Cancelled):
        return answer
    if answer.text == "-":
        return None
    return answer.text or current


def _topics_page(dsn: str | None) -> None:
    at, note, selected = 0, "", None
    while True:
        rows = _topic_rows(dsn)
        kept = next((index for index, row in enumerate(rows) if row["topic_id"] == selected), None)
        if kept is not None:
            at = kept
        selected = None
        at = max(0, min(at, len(rows) - 1))
        lines = []
        for index, row in enumerate(rows):
            line = screen.clip(
                f"    {screen.pad(row['name'], 24)} "
                f"{screen.pad(row.get('scope_name') or 'every session', 16)}"
                f"{_rules(row['rules']):>9}  ·  {row['body_tokens']:>4} tokens"
                + (f"  ·  before: {row['action']}" if row.get("action") else ""),
                screen.terminal_width(),
            )
            lines.append(screen.selected(line) if index == at else line)
        detail = _topic_detail(rows[at]) if rows else "  No topics yet. Press n to create one."
        keys = screen.trailer(
            detail,
            "  n new topic   e edit   x remove   " + _LIST_KEYS.strip(),
            note,
        )
        screen.paint(screen.list_screen(_title("Topics"), lines, at, keys))
        key = screen.getkey()
        note = ""
        if key in ("q", "left"):
            return
        if key == "?":
            _help()
        elif key in ("up", "k"):
            at = max(0, at - 1)
        elif key in ("down", "j"):
            at = min(max(0, len(rows) - 1), at + 1)
        elif key == "home":
            at = 0
        elif key == "end":
            at = max(0, len(rows) - 1)
        elif key == "e":
            if not rows:
                note = "  there is no topic to edit"
                continue
            row = rows[at]
            selected = row["topic_id"]
            name = _replacement("topic name", row["name"], clearable=False)
            trigger = _replacement("trigger", row["trigger"], clearable=False)
            scope_name = _topic_scope("scope", row.get("scope_name"))
            action = _replacement(
                "action, the tool calls whose start shows its rules "
                "(delegate = Agent / Task subagent calls)",
                row.get("action"),
            )
            answers = (name, trigger, scope_name, action)
            if any(isinstance(value, screen.Cancelled) for value in answers):
                note = "  topic unchanged"
                continue
            try:
                with db.transaction(dsn) as cur:
                    scope_id = (
                        scopes.require_scope(cur, scope_name)["scope_id"] if scope_name else None
                    )
                    topics.update_topic(
                        cur,
                        row["topic_id"],
                        actor=ACTOR,
                        name=name or row["name"],
                        trigger=trigger or row["trigger"],
                        scope_id=scope_id,
                        clear_scope=scope_id is None,
                        action=action,
                        clear_action=action is None,
                    )
                note = f"  edited topic {name or row['name']}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "x":
            if not rows:
                note = "  there is no topic to remove"
                continue
            row = rows[at]
            if not _confirm(f"remove topic {row['name']}?"):
                note = "  topic kept"
                continue
            try:
                with db.transaction(dsn) as cur:
                    removed = topics.remove_topic(cur, row["topic_id"], actor=ACTOR)
                note = f"  {removed['removed']} topic {row['name']}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "n":
            name_answer = _answer("  topic name: ")
            if isinstance(name_answer, screen.Cancelled):
                note = "  topic creation cancelled"
                continue
            if not name_answer.text:
                note = "  a topic needs a name"
                continue
            scope_answer = _answer("  scope [empty means every session]: ")
            if isinstance(scope_answer, screen.Cancelled):
                note = "  topic creation cancelled"
                continue
            trigger_answer = _answer("  trigger [one sentence saying when to read it]: ")
            if isinstance(trigger_answer, screen.Cancelled):
                note = "  topic creation cancelled"
                continue
            try:
                with db.transaction(dsn) as cur:
                    topics.create_topic(
                        cur,
                        name=name_answer.text,
                        trigger=trigger_answer.text,
                        scope_id=scopes.require_scope(cur, scope_answer.text)["scope_id"]
                        if scope_answer.text
                        else None,
                        actor=ACTOR,
                    )
                note = f"  created topic {name_answer.text}"
            except MashuError as error:
                note = f"  {error}"


def _route_rows(dsn: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return routing.all_routes(cur)


def _route_line(row: dict[str, Any]) -> str:
    destination = row.get("scope_name") or "ignored"
    return screen.clip(f"    {row['path_prefix']}  →  {destination}", screen.terminal_width())


def _routes_page(dsn: str | None) -> None:
    at, note, selected = 0, "", None
    while True:
        rows = _route_rows(dsn)
        kept = next((index for index, row in enumerate(rows) if row["route_id"] == selected), None)
        if kept is not None:
            at = kept
        selected = None
        at = max(0, min(at, len(rows) - 1))
        lines = [
            screen.selected(_route_line(row)) if index == at else _route_line(row)
            for index, row in enumerate(rows)
        ]
        if rows:
            destination = (
                "ignored intentionally"
                if rows[at]["scope_id"] is None
                else f"scope {rows[at]['scope_name']}"
            )
            detail = f"  ── {rows[at]['path_prefix']}\n  {destination}"
        else:
            detail = "  No routes yet. Add a scoped route with n or an ignored path with i."
        keys = screen.trailer(
            detail,
            "  n scoped route   i ignore path   e edit   x remove   " + _LIST_KEYS.strip(),
            note,
        )
        screen.paint(screen.list_screen(_title("Routes"), lines, at, keys))
        key = screen.getkey()
        note = ""
        if key in ("q", "left"):
            return
        if key == "?":
            _help()
        elif key in ("up", "k"):
            at = max(0, at - 1)
        elif key in ("down", "j"):
            at = min(max(0, len(rows) - 1), at + 1)
        elif key == "home":
            at = 0
        elif key == "end":
            at = max(0, len(rows) - 1)
        elif key == "e":
            if not rows:
                note = "  there is no route to edit"
                continue
            row = rows[at]
            selected = row["route_id"]
            path = _replacement("directory path", row["path_prefix"], clearable=False)
            scope_name = _replacement("scope", row.get("scope_name"))
            if isinstance(path, screen.Cancelled) or isinstance(scope_name, screen.Cancelled):
                note = "  route unchanged"
                continue
            try:
                with db.transaction(dsn) as cur:
                    application.update_route(
                        cur,
                        row["route_id"],
                        path_prefix=path or row["path_prefix"],
                        scope_name=scope_name,
                        actor=ACTOR,
                    )
                destination = scope_name or "ignored"
                edited_path = routing.normalise(path or row["path_prefix"])
                note = f"  edited route {edited_path} → {destination}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "n":
            path_answer = _answer("  directory path: ")
            if isinstance(path_answer, screen.Cancelled):
                note = "  route creation cancelled"
                continue
            if not path_answer.text:
                note = "  a scoped route needs a path and a scope"
                continue
            scope_answer = _answer("  scope name: ")
            if isinstance(scope_answer, screen.Cancelled):
                note = "  route creation cancelled"
                continue
            if not scope_answer.text:
                note = "  a scoped route needs both a path and a scope"
                continue
            try:
                with db.transaction(dsn) as cur:
                    application.set_route(
                        cur,
                        path_prefix=path_answer.text,
                        scope_name=scope_answer.text,
                        actor=ACTOR,
                    )
                note = f"  routed {routing.normalise(path_answer.text)} to {scope_answer.text}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "i":
            path_answer = _answer("  directory path to ignore: ")
            if isinstance(path_answer, screen.Cancelled):
                note = "  route creation cancelled"
                continue
            if not path_answer.text:
                note = "  an ignored route needs a path"
                continue
            try:
                with db.transaction(dsn) as cur:
                    application.ignore_route(cur, path_prefix=path_answer.text, actor=ACTOR)
                note = f"  ignoring {routing.normalise(path_answer.text)}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "x":
            if not rows:
                note = "  there is no route to remove"
                continue
            path = rows[at]["path_prefix"]
            if not _confirm(f"remove route {path}?"):
                note = "  route kept"
                continue
            try:
                with db.transaction(dsn) as cur:
                    removed = application.remove_route(cur, path_prefix=path, actor=ACTOR)
                note = f"  removed {path}" if removed else f"  no route {path}"
            except MashuError as error:
                note = f"  {error}"


def _schema_pending(dsn: str | None) -> list[str]:
    with db.transaction(dsn) as cur:
        return migration.pending(cur)


def _schema_page(dsn: str | None) -> None:
    note = ""
    while True:
        pending = _schema_pending(dsn)
        if pending:
            rows = "\n".join(f"    {index}. {name}" for index, name in enumerate(pending, 1))
            body = f"  {len(pending)} migration(s) pending\n\n{rows}"
        else:
            body = f"  {screen.success('Schema is up to date.')}"
        if note:
            body += f"\n\n{note}"
        keys = "  m apply pending migrations   ←/q settings   ? help"
        screen.paint(f"{_title('Schema/migrations')}\n\n{body}\n\n{keys}")
        key = screen.getkey()
        note = ""
        if key in ("q", "left"):
            return
        if key == "?":
            _help()
        elif key == "m":
            count = len(pending)
            if not _confirm(f"apply {count} pending migration(s)?"):
                note = "  schema unchanged"
                continue
            try:
                applied = migration.migrate(dsn)
                note = (
                    f"  applied {len(applied)}: {', '.join(applied)}"
                    if applied
                    else "  schema was already up to date"
                )
            except MashuError as error:
                note = f"  {error}"


def _top_text(at: int, note: str = "") -> str:
    rows = []
    for index, (label, detail) in enumerate(_TOP_ITEMS):
        line = screen.clip(f"    {screen.pad(label, 23)} {detail}", screen.terminal_width())
        rows.append(screen.selected(line) if index == at else line)
    keys = screen.trailer(_TOP_KEYS, note)
    subtitle = screen.dim("Inspect the store, then change only what you choose.")
    return screen.list_screen(
        f"{_title('Settings & health')}\n{subtitle}",
        rows,
        at,
        keys,
    )


def _health_page(dsn: str | None) -> None:
    _read_page("Health/status", _health_text(_health(dsn)))


def _bootstrap_page(dsn: str | None) -> None:
    _read_page("Bootstrap preview", _bootstrap_text(_bootstrap_preview(dsn)))


_PAGES: tuple[Callable[[str | None], None], ...] = (
    _health_page,
    _bootstrap_page,
    _scopes_page,
    _topics_page,
    _routes_page,
    _schema_page,
)


@screen.fullscreen
def run(dsn: str | None = None) -> int:
    """Open settings, keeping every mutation explicit and every page escapable."""
    at, note = 0, ""
    while True:
        screen.paint(_top_text(at, note))
        key = screen.getkey()
        note = ""
        if key in ("q", "left"):
            return 0
        if key == "?":
            _help()
        elif key in ("up", "k"):
            at = max(0, at - 1)
        elif key in ("down", "j"):
            at = min(len(_TOP_ITEMS) - 1, at + 1)
        elif key == "home":
            at = 0
        elif key == "end":
            at = len(_TOP_ITEMS) - 1
        elif key == "enter":
            try:
                _PAGES[at](dsn)
            except MashuError as error:
                note = f"  {error}"
