"""Settings and store-health screens for people running Mashu interactively."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mashu import (
    bootstrap,
    capacity,
    config,
    db,
    routing,
    scopes,
    screen,
    tasks,
    temporary,
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

  Scope and route pages show their own write keys. Route removal and schema
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
    scope_count: int
    tasks_active: int
    tasks_dormant: int
    tasks_closed: int


def _health(dsn: str | None) -> Health:
    """Collect the status page in one consistent database snapshot."""
    with db.transaction(dsn) as cur:
        schema_pending = tuple(migration.pending(cur))
        cur.execute(
            """
            SELECT delivery, count(*) AS n FROM memory
            WHERE status = 'active' GROUP BY delivery ORDER BY delivery
            """
        )
        memory_counts = {row["delivery"]: row["n"] for row in cur.fetchall()}
        memory_totals = capacity.bootstrap_totals(cur)
        state_totals = tasks.pushed_totals(cur)
        temporary_totals = temporary.pushed_totals(cur)

        cur.execute(
            """
            SELECT count(*) FILTER (WHERE deferred_at IS NULL) AS ready,
                   count(*) FILTER (WHERE deferred_at IS NOT NULL) AS deferred
            FROM nomination WHERE status = 'pending'
            """
        )
        pending = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM trace WHERE expires_at > now()")
        traces = cur.fetchone()["n"]
        cur.execute(
            "SELECT count(*) AS n FROM ledger WHERE created_at >= now() - interval '30 days'"
        )
        ledger_30d = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT count(*) AS n FROM event_log
            WHERE event_type = 'delivery_failure_suspected'
              AND created_at >= now() - interval '30 days'
            """
        )
        delivery_failures = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM scope")
        scope_count = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT count(*) FILTER (
                       WHERE status = 'open' AND now() <= active_until
                   ) AS active,
                   count(*) FILTER (
                       WHERE status = 'open' AND now() > active_until
                   ) AS dormant,
                   count(*) FILTER (WHERE status = 'closed') AS closed
            FROM task
            """
        )
        task_counts = cur.fetchone()

    return Health(
        schema_pending=schema_pending,
        memory_counts=memory_counts,
        memory_always_tokens=memory_totals["always"],
        memory_worst_tokens=memory_totals["worst"],
        active_states=state_totals["count"],
        state_worst_tokens=state_totals["worst"],
        temporary_count=temporary_totals["count"],
        temporary_tokens=temporary_totals["worst"],
        pending_ready=pending["ready"],
        pending_deferred=pending["deferred"],
        traces=traces,
        ledger_30d=ledger_30d,
        delivery_failures_30d=delivery_failures,
        scope_count=scope_count,
        tasks_active=task_counts["active"],
        tasks_dormant=task_counts["dormant"],
        tasks_closed=task_counts["closed"],
    )


def _health_text(value: Health) -> str:
    if value.schema_pending:
        schema = screen.warning(
            f"{len(value.schema_pending)} pending: {', '.join(value.schema_pending)}"
        )
    else:
        schema = screen.success("up to date")
    deliveries = "  ·  ".join(
        f"{name} {value.memory_counts.get(name, 0)}" for name in ("always", "scope", "guard")
    )
    return "\n".join(
        (
            f"  Schema             {schema}",
            "",
            f"  Active memories    {deliveries}",
            f"  Memory capacity    always {value.memory_always_tokens}/{config.always_capacity()}"
            f"  ·  worst {value.memory_worst_tokens}/{config.capacity()}",
            f"  Project state      {value.active_states} active"
            f"  ·  worst {value.state_worst_tokens}/{config.project_capacity()} tokens",
            f"  Temporary          {value.temporary_count} standing"
            f"  ·  {value.temporary_tokens}/{config.temporary_capacity()} tokens",
            "",
            f"  Review queue       {value.pending_ready} ready"
            f"  ·  {value.pending_deferred} deferred",
            f"  Evidence           {value.traces} live traces"
            f"  ·  {value.ledger_30d} ledger entries in 30 days",
            f"  Delivery health    {value.delivery_failures_30d} suspected failures in 30 days",
            f"  Scopes             {value.scope_count}",
            f"  Tasks              {value.tasks_active} active"
            f"  ·  {value.tasks_dormant} dormant  ·  {value.tasks_closed} closed",
        )
    )


def _bootstrap_preview(dsn: str | None) -> dict[str, Any]:
    """Build the same payload a session started in the current directory receives."""
    with db.transaction(dsn) as cur:
        scope_id, routed = routing.resolve(cur, os.getcwd())
        scope_name = None
        if scope_id is not None:
            cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
            row = cur.fetchone()
            scope_name = row["name"] if row else None
        return bootstrap.session_bootstrap(
            cur,
            actor=ACTOR,
            scope_id=scope_id,
            scope_name=scope_name,
            routed=routed,
        )


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

    states = answer.get("states") or []
    lines.append(f"  Current project state ({len(states)})")
    if not states:
        lines.append("    —")
    for row in states:
        lines.append(f"    {row['task']}  {row['heading']}")
        lines.append(screen.wrap(row["content"], indent="      "))

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
            f"  ·  state {answer.get('project_tokens', 0)}"
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


def _answer(prompt: str, *, required: bool = False) -> str | None:
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if required and not value:
        return None
    return value or None


def _confirm(question: str) -> bool:
    try:
        return input(f"  {question} [y/N]: ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False


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
    at, note = 0, ""
    while True:
        rows = _scope_rows(dsn)
        at = max(0, min(at, len(rows) - 1))
        lines = []
        for index, row in enumerate(rows):
            line = screen.clip(
                f"    {screen.pad(row['name'], 24)} {row['n_active']:>3} memories"
                f"  ·  {row['push_tokens']:>4} tokens",
                screen.text_width(),
            )
            lines.append(screen.selected(line) if index == at else line)
        detail = _scope_detail(rows[at]) if rows else "  No scopes yet. Press n to create one."
        keys = screen.trailer(
            detail,
            "  n new scope   " + _LIST_KEYS.strip(),
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
        elif key == "n":
            name = _answer("  scope name: ", required=True)
            if name is None:
                note = "  a scope needs a name"
                continue
            summary = _answer("  about [optional]: ")
            try:
                with db.transaction(dsn) as cur:
                    scopes.create_scope(cur, name=name, summary=summary, actor=ACTOR)
                note = f"  created scope {name}"
            except MashuError as error:
                note = f"  {error}"


def _route_rows(dsn: str | None) -> list[dict[str, Any]]:
    with db.transaction(dsn) as cur:
        return routing.all_routes(cur)


def _route_line(row: dict[str, Any]) -> str:
    destination = row.get("scope_name") or "ignored"
    return screen.clip(f"    {row['path_prefix']}  →  {destination}", screen.text_width())


def _routes_page(dsn: str | None) -> None:
    at, note = 0, ""
    while True:
        rows = _route_rows(dsn)
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
            "  n scoped route   i ignore path   x remove   " + _LIST_KEYS.strip(),
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
        elif key == "n":
            path = _answer("  directory path: ", required=True)
            scope_name = _answer("  scope name: ", required=True)
            if path is None or scope_name is None:
                note = "  a scoped route needs both a path and a scope"
                continue
            try:
                with db.transaction(dsn) as cur:
                    scope = scopes.require_scope(cur, scope_name)
                    routing.add_route(
                        cur, path_prefix=path, scope_id=scope["scope_id"], actor=ACTOR
                    )
                note = f"  routed {routing.normalise(path)} to {scope_name}"
            except MashuError as error:
                note = f"  {error}"
        elif key == "i":
            path = _answer("  directory path to ignore: ", required=True)
            if path is None:
                note = "  an ignored route needs a path"
                continue
            try:
                with db.transaction(dsn) as cur:
                    routing.add_route(cur, path_prefix=path, scope_id=None, actor=ACTOR)
                note = f"  ignoring {routing.normalise(path)}"
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
                    removed = routing.remove_route(cur, path_prefix=path, actor=ACTOR)
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
        line = screen.clip(f"    {screen.pad(label, 23)} {detail}", screen.text_width())
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
