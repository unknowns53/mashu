"""Read-only web pages for surveying the whole store at once in a browser."""

from __future__ import annotations

import datetime as dt
import html
import json
import sys
import webbrowser
from collections.abc import Callable, Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import psycopg

from mashu import (
    application,
    config,
    db,
    memories,
    memory_changes,
    nominations,
    projects,
    task_history,
    tasks,
)
from mashu.errors import MashuError

HOST = "127.0.0.1"
DEFAULT_PORT = 8642

#: The task listings a person can pick, in the order the picker shows them.
TASK_ACTIVITIES = ("open", "active", "dormant", "closed", "all")

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1d1d1f; --muted: #6b6b70; --line: #dcdcdf;
  --head: #f0f0f2; --accent: #0b62c4; --warn: #a3510c; --danger: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17171a; --fg: #e6e6e9; --muted: #9a9aa2; --line: #34343a;
    --head: #222227; --accent: #6aa8ff; --warn: #e3a35f; --danger: #ff8a80;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans", sans-serif; }
header { position: sticky; top: 0; background: var(--head); border-bottom: 1px solid var(--line);
  padding: 8px 16px; display: flex; gap: 18px; align-items: baseline; flex-wrap: wrap; }
header strong { margin-right: 12px; }
header a.here { font-weight: 600; text-decoration: underline; }
header .note { margin-left: auto; color: var(--muted); font-size: 12px; }
main { padding: 12px 16px 48px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { font-size: 20px; margin: 8px 0 12px; }
h2 { font-size: 16px; margin: 24px 0 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid var(--line); padding: 4px 8px; vertical-align: top; text-align: left; }
th { background: var(--head); position: sticky; top: 41px; font-weight: 600; }
td.text { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 14em; }
td.nowrap, .mono { white-space: nowrap; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.muted { color: var(--muted); }
.warn { color: var(--warn); }
.danger { color: var(--danger); }
.block { white-space: pre-wrap; overflow-wrap: anywhere; border-left: 3px solid var(--line);
  padding: 4px 10px; margin: 4px 0 12px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0 0 12px; }
dt { color: var(--muted); }
dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
form { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; margin-bottom: 12px; }
label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); }
input[type=search] { min-width: 14em; }
ul.items { margin: 0; padding-left: 1.2em; }
"""

#: Every page is plain HTML: no script runs, and only the inline stylesheet loads.
_HEADERS = (
    ("Content-Type", "text/html; charset=utf-8"),
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)

_NAV = (
    ("/", "Overview"),
    ("/memories", "Memories"),
    ("/temporary", "Temporary"),
    ("/tasks", "Tasks"),
    ("/review", "Review"),
)


class NotFound(Exception):
    """No page, or no row, answers to the path asked for."""


# text


def _e(value: Any) -> str:
    """One stored value as page text. Everything from the store passes through here."""
    if value is None or value == "":
        return "-"
    if isinstance(value, dt.datetime):
        return html.escape(value.astimezone().strftime("%Y-%m-%d %H:%M"))
    if isinstance(value, dict | list):
        return html.escape(json.dumps(value, ensure_ascii=False, default=str))
    return html.escape(str(value))


def _short(value: Any) -> str:
    return html.escape(str(value)[:8])


def _href(path: str, **query: str) -> str:
    kept = {key: value for key, value in query.items() if value}
    return html.escape(f"{path}?{urlencode(kept)}" if kept else path)


def _memory_link(memory_id: Any) -> str:
    if not memory_id:
        return "-"
    return f'<a class="mono" href="{_href(f"/memories/{memory_id}")}">{_short(memory_id)}</a>'


def _task_link(task_id: Any) -> str:
    return f'<a class="mono" href="{_href(f"/tasks/{task_id}")}">{_short(task_id)}</a>'


def _items(values: Iterable[Any]) -> str:
    listed = [f"<li>{_e(value)}</li>" for value in values]
    return f'<ul class="items">{"".join(listed)}</ul>' if listed else "-"


def _fields(pairs: Iterable[tuple[str, str]]) -> str:
    """A definition list; each value must already be escaped markup."""
    return (
        "<dl>"
        + "".join(f"<dt>{_e(label)}</dt><dd>{value}</dd>" for label, value in pairs)
        + "</dl>"
    )


def _table(headings: Iterable[str], rows: Iterable[Iterable[str]], *, empty: str) -> str:
    """A table whose cells are already escaped markup."""
    body = ["<tr>" + "".join(cells) + "</tr>" for cells in rows]
    if not body:
        return f'<p class="muted">{_e(empty)}</p>'
    head = "".join(f"<th>{_e(heading)}</th>" for heading in headings)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _td(markup: str, kind: str = "") -> str:
    return f'<td class="{kind}">{markup}</td>' if kind else f"<td>{markup}</td>"


def _select(name: str, current: str, options: Iterable[tuple[str, str]]) -> str:
    choices = "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == current else ""}>'
        f"{_e(label)}</option>"
        for value, label in options
    )
    return f'<label>{_e(name)}<select name="{html.escape(name)}">{choices}</select></label>'


def _search(current: str) -> str:
    return (
        f'<label>search<input type="search" name="q" value="{html.escape(current)}"></label>'
        '<button type="submit">Filter</button>'
    )


def _page(title: str, body: str, *, here: str) -> str:
    current = ' class="here"'
    nav = "".join(
        f'<a href="{path}"{current if path == here else ""}>{label}</a>' for path, label in _NAV
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)} · Mashu</title><style>{_STYLE}</style></head><body>"
        f'<header><strong>Mashu</strong>{nav}<span class="note">read-only; '
        "changes are made in the terminal UI</span></header>"
        f"<main><h1>{_e(title)}</h1>{body}</main></body></html>"
    )


def _choice(query: dict[str, str], name: str, allowed: Iterable[str]) -> str:
    """The value asked for, or the first allowed one when it is not one of them."""
    allowed = tuple(allowed)
    value = query.get(name, "")
    return value if value in allowed else allowed[0]


def _matches(needle: str, *haystack: Any) -> bool:
    folded = needle.casefold()
    return any(folded in str(value).casefold() for value in haystack if value)


# pages


def _overview(cur: psycopg.Cursor, query: dict[str, str]) -> str:
    status = application.status_snapshot(cur)
    changes = len(memory_changes.pending(cur))
    active = sum(status.memory_counts.values())
    by_delivery = "  ".join(
        f"{delivery} {status.memory_counts.get(delivery, 0)}" for delivery in memories.DELIVERIES
    )
    rows = [
        ("Memories (active)", "/memories", active, by_delivery),
        ("Memories (retired)", _href("/memories", status="retired"), None, ""),
        (
            "Temporary Context",
            "/temporary",
            status.temporary_count,
            f"{status.temporary_tokens}/{config.temporary_capacity()} tokens",
        ),
        ("Tasks (active)", _href("/tasks", activity="active"), status.tasks_active, ""),
        ("Tasks (dormant)", _href("/tasks", activity="dormant"), status.tasks_dormant, ""),
        ("Tasks (closed)", _href("/tasks", activity="closed"), status.tasks_closed, ""),
        ("Candidates ready", "/review", status.pending_ready, ""),
        ("Candidates put off", _href("/review", deferred="1"), status.pending_deferred, ""),
        ("Memory changes pending", "/review", changes, ""),
    ]
    linked = [
        [
            _td(f'<a href="{href}">{_e(label)}</a>'),
            _td(_e(count) if count is not None else "", "nowrap"),
            _td(_e(note) if note else ""),
        ]
        for label, href, count, note in rows
    ]
    failures = "  ".join(
        f"{route} {status.delivery_failures_by_route.get(route, 0)}"
        for route in application.DELIVERY_FAILURE_ROUTES
    )
    store = [
        (
            "schema",
            f'<span class="danger">{_e(len(status.schema_pending))} migration(s) pending: '
            f"{_e(', '.join(status.schema_pending))}</span>"
            if status.schema_pending
            else "up to date",
        ),
        (
            "Memory tokens",
            _e(
                f"always {status.memory_always_tokens}/{config.always_capacity()}  "
                f"worst {status.memory_worst_tokens}/{config.capacity()}"
            ),
        ),
        (
            "Task cards",
            _e(
                f"{status.active_states} active  "
                f"worst {status.state_worst_tokens}/{config.project_capacity()} tokens"
            ),
        ),
        ("Traces (unexpired)", _e(status.traces)),
        ("Ledger (30 days)", _e(status.ledger_30d)),
        ("Delivery failures (30 days)", _e(f"{status.delivery_failures_30d}  ({failures})")),
        ("Scopes", _e(", ".join(row["name"] for row in status.scope_rows) or "none")),
        (
            "Topics",
            _e(
                f"{status.topic_count}"
                + (
                    f"  heaviest {status.topic_heaviest_name} "
                    f"{status.topic_heaviest_tokens}/{config.topic_capacity()}"
                    if status.topic_heaviest_name
                    else ""
                )
            ),
        ),
    ]
    return (
        _table(("area", "count", ""), linked, empty="nothing stored")
        + "<h2>Store</h2>"
        + _fields(store)
    )


def _memory_list(cur: psycopg.Cursor, query: dict[str, str]) -> str:
    status = _choice(query, "status", ("active", "retired"))
    delivery = _choice(query, "delivery", ("", *memories.DELIVERIES))
    every = application.memory_view(cur, status)
    scope_names = sorted({row["scope_name"] for row in every if row["scope_name"]})
    topic_names = sorted({row["topic_name"] for row in every if row["topic_name"]})
    scope = _choice(query, "scope", ("", *scope_names))
    topic = _choice(query, "topic", ("", *topic_names))
    needle = query.get("q", "")
    shown = [
        row
        for row in every
        if (not delivery or row["delivery"] == delivery)
        and (not scope or row["scope_name"] == scope)
        and (not topic or row["topic_name"] == topic)
        and (not needle or _matches(needle, row["content"], row["memory_id"]))
    ]
    form = (
        '<form method="get" action="/memories">'
        + _select("status", status, (("active", "active"), ("retired", "retired")))
        + _select("delivery", delivery, (("", "all"), *((d, d) for d in memories.DELIVERIES)))
        + _select("scope", scope, (("", "all"), *((name, name) for name in scope_names)))
        + _select("topic", topic, (("", "all"), *((name, name) for name in topic_names)))
        + _search(needle)
        + "</form>"
    )
    retired = status == "retired"
    headings = ["id", "delivery", "scope", "topic", "content", "created", "updated"]
    if retired:
        headings[5:5] = ["retired as", "reason"]
    rows = []
    for row in shown:
        cells = [
            _td(_memory_link(row["memory_id"]), "nowrap"),
            _td(_e(row["delivery"]), "nowrap"),
            _td(_e(row["scope_name"])),
            _td(_e(row["topic_name"])),
            _td(_e(row["content"]), "text"),
            _td(_e(row["created_at"]), "nowrap"),
            _td(_e(row["updated_at"]), "nowrap"),
        ]
        if retired:
            cells[5:5] = [
                _td(_e(row["retirement_kind"] or "legacy"), "nowrap"),
                _td(_e(row["retire_reason"]), "text"),
            ]
        rows.append(cells)
    count = f'<p class="muted">{len(shown)} of {len(every)} {status} Memories</p>'
    return form + count + _table(headings, rows, empty="no Memories match")


def _memory_detail(cur: psycopg.Cursor, memory_id: UUID) -> tuple[str, str]:
    row = memories.memory_details(cur, memory_id)
    if row is None:
        raise NotFound(f"no Memory {memory_id}")
    topic = _e(row["topic_name"])
    if row["topic_trigger"]:
        topic += f' <span class="muted">({_e(row["topic_trigger"])})</span>'
    fields = [
        ("id", f'<span class="mono">{_e(row["memory_id"])}</span>'),
        ("status", _e(row["status"])),
        ("delivery", _e(row["delivery"])),
        ("scope", _e(row["scope_name"])),
        ("topic", topic),
        ("created", f"{_e(row['created_at'])}  {_e(row['created_by'])}"),
        ("updated", _e(row["updated_at"])),
        ("current revision", f'<span class="mono">{_e(row["current_revision_id"])}</span>'),
    ]
    retirement = row["retirement"]
    if retirement:
        fields += [
            ("retired as", f'<span class="warn">{_e(retirement["kind"])}</span>'),
            ("retired at", _e(retirement["retired_at"])),
            ("reason", _e(retirement["reason"])),
            ("successor", _memory_link(retirement["superseded_by"])),
        ]
        if retirement["relocated_to_id"]:
            fields.append(
                (
                    "moved to",
                    f"{_e(retirement['relocated_to_kind'])} "
                    f'<span class="mono">{_e(retirement["relocated_to_id"])}</span>',
                )
            )
    evidence = _table(
        ("kind", "recorded", "what", "prevention"),
        (
            [
                _td(_e(item["kind"]), "nowrap"),
                _td(_e(item["created_at"]), "nowrap"),
                _td(_e(item["what"]), "text"),
                _td(_e(item["prevention"]), "text"),
            ]
            for item in row["basis"]
        ),
        empty="no evidence rows",
    )
    revisions = _table(
        ("written", "by", "note", "content"),
        (
            [
                _td(_e(item["created_at"]), "nowrap"),
                _td(_e(item["actor"]), "nowrap"),
                _td(_e(item["note"]), "text"),
                _td(_e(item["content"]), "text"),
            ]
            for item in row["revisions"]
        ),
        empty="no revisions",
    )
    history = _table(
        ("when", "event", "by", "detail"),
        (
            [
                _td(_e(item["created_at"]), "nowrap"),
                _td(_e(item["event_type"]), "nowrap"),
                _td(_e(item["actor"]), "nowrap"),
                _td(_e(item["detail"]), "text"),
            ]
            for item in row["retirement_history"]
        ),
        empty="never retired or restored",
    )
    body = (
        f'<div class="block">{_e(row["content"])}</div>'
        + _fields(fields)
        + "<h2>Evidence</h2>"
        + evidence
        + "<h2>Revisions</h2>"
        + revisions
        + "<h2>Retirement history</h2>"
        + history
    )
    return f"Memory {str(memory_id)[:8]}", body


def _temporary(cur: psycopg.Cursor, query: dict[str, str]) -> str:
    rows = application.memory_view(cur, "temporary")
    return _table(
        ("id", "scope", "content", "by", "created", "expires"),
        (
            [
                _td(_short(row["context_id"]), "mono"),
                _td(_e(row["scope_name"] or "every session")),
                _td(_e(row["content"]), "text"),
                _td(_e(row["created_by"]), "nowrap"),
                _td(_e(row["created_at"]), "nowrap"),
                _td(_e(row["expires_at"]), "nowrap"),
            ]
            for row in rows
        ),
        empty="no Temporary Context in force",
    )


def _proposal_text(proposal: dict[str, Any] | None) -> str:
    if not proposal:
        return ""
    stale = ' <span class="warn">(state changed since)</span>' if proposal["stale"] else ""
    return f"{_e(proposal['outcome'])}: {_e(proposal['reason'])}{stale}"


def _task_list(cur: psycopg.Cursor, query: dict[str, str]) -> str:
    activity = _choice(query, "activity", TASK_ACTIVITIES)
    names = [row["name"] for row in projects.list_projects(cur, include_archived=True)]
    project = _choice(query, "project", ("", *names))
    needle = query.get("q", "")
    every = tasks.task_list(cur, project=project or None, activity=activity)
    shown = [
        row
        for row in every
        if not needle
        or _matches(
            needle,
            row["task"]["task_id"],
            row["task"]["name"],
            row["state"]["goal"],
            row["state"]["status_text"],
        )
    ]
    form = (
        '<form method="get" action="/tasks">'
        + _select("activity", activity, ((name, name) for name in TASK_ACTIVITIES))
        + _select("project", project, (("", "all"), *((name, name) for name in names)))
        + _search(needle)
        + "</form>"
    )
    rows = []
    for row in shown:
        task, state = row["task"], row["state"]
        standing = row["activity"]
        if task["status"] == "closed":
            standing = f"closed {task['outcome'] or ''}".strip()
        rows.append(
            [
                _td(_task_link(task["task_id"]), "nowrap"),
                _td(_e(task["name"]), "text"),
                _td(_e(task["project_name"])),
                _td(_e(standing), "nowrap"),
                _td(_e(state["goal"]), "text"),
                _td(_e(state["status_text"]), "text"),
                _td(_proposal_text(row["proposal"]), "text"),
                _td(_e(state["updated_at"]), "nowrap"),
            ]
        )
    count = f'<p class="muted">{len(shown)} of {len(every)} {activity} tasks</p>'
    headings = ("id", "name", "project", "activity", "goal", "status", "close proposal", "updated")
    return form + count + _table(headings, rows, empty="no tasks match")


def _state_fields(state: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("goal", _e(state["goal"])),
        ("approach", _e(state["approach"])),
        ("status", _e(state["status_text"])),
        *((field.replace("_", " "), _items(state[field] or [])) for field in tasks.LIST_FIELDS),
    ]


def _task_detail(cur: psycopg.Cursor, task_id: UUID) -> tuple[str, str]:
    try:
        row = task_history.expanded_task(
            cur, task_id, attempts=True, decisions=True, artifacts=True, checkpoints=True
        )
    except MashuError as error:
        raise NotFound(str(error)) from error
    task, state = row["task"], row["state"]
    if task["status"] == "closed":
        standing = f"closed {_e(task['outcome'])}: {_e(task['close_reason'])}"
    else:
        standing = f"open {_e(row['activity'])}, lease to {_e(task['active_until'])}"
    fields = [
        ("id", f'<span class="mono">{_e(task["task_id"])}</span>'),
        ("project", _e(task["project_name"])),
        ("status", standing),
        ("created", f"{_e(task['created_at'])}  {_e(task['created_by'])}"),
        ("last activity", _e(task["last_activity_at"])),
    ]
    proposal = row["proposal"]
    if proposal:
        fields.append(
            (
                "close proposed",
                f"{_proposal_text(proposal)} "
                f'<span class="muted">{_e(proposal["proposed_by"])} '
                f"{_e(proposal['proposed_at'])}</span>",
            )
        )
    current = _fields(
        [
            *_state_fields(state),
            ("updated", f"{_e(state['updated_at'])}  {_e(state['updated_by'])}"),
        ]
    )
    artifacts = {item["reference_id"]: item for item in row["artifacts"]}
    # Locators are shown as text, never as links: an agent wrote them, and a link would open
    # whatever scheme it names.
    artifact_table = _table(
        ("id", "recorded", "kind", "locator", "label"),
        (
            [
                _td(_short(item["reference_id"]), "mono"),
                _td(_e(item["created_at"]), "nowrap"),
                _td(_e(item["kind"]), "nowrap"),
                _td(_e(item["locator"]), "text"),
                _td(_e(item["label"]), "text"),
            ]
            for item in row["artifacts"]
        ),
        empty="no artifacts",
    )

    def cited(reference_id: Any) -> str:
        item = artifacts.get(reference_id)
        if item is None:
            return _short(reference_id)
        return f"{_e(item['kind'])} {_e(item['locator'])}"

    checkpoint_table = _table(
        ("when", "by", "what changed", "evidence", "state then"),
        (
            [
                _td(_e(point["created_at"]), "nowrap"),
                _td(_e(point["created_by"]), "nowrap"),
                _td(_e(point["what_changed"]), "text"),
                _td(
                    '<ul class="items">'
                    + "".join(f"<li>{cited(ref)}</li>" for ref in point["evidence"])
                    + "</ul>"
                    if point["evidence"]
                    else "-"
                ),
                _td(f"<details><summary>show</summary>{_fields(_state_fields(point))}</details>"),
            ]
            for point in row["checkpoints"]
        ),
        empty="no checkpoints",
    )
    attempt_table = _table(
        ("when", "by", "attempt", "result", "reason", "next"),
        (
            [
                _td(_e(item["created_at"]), "nowrap"),
                _td(_e(item["created_by"]), "nowrap"),
                _td(_e(item["attempt"]), "text"),
                _td(_e(item["result"]), "text"),
                _td(_e(item["reason"]), "text"),
                _td(_e(item["next"]), "text"),
            ]
            for item in row["attempts"]
        ),
        empty="no attempts",
    )
    decision_table = _table(
        ("when", "by", "decision", "reason", "supersedes"),
        (
            [
                _td(_e(item["created_at"]), "nowrap"),
                _td(_e(item["created_by"]), "nowrap"),
                _td(_e(item["decision"]), "text"),
                _td(_e(item["reason"]), "text"),
                _td(_short(item["supersedes_id"]) if item["supersedes_id"] else "-", "mono"),
            ]
            for item in row["decisions"]
        ),
        empty="no decisions",
    )
    body = (
        _fields(fields)
        + f"<h2>Current state</h2>{current}"
        + f"<h2>Checkpoints</h2>{checkpoint_table}"
        + f"<h2>Attempts</h2>{attempt_table}"
        + f"<h2>Decisions</h2>{decision_table}"
        + f"<h2>Artifacts</h2>{artifact_table}"
    )
    return f"Task {task['name']}", body


def _conflicts(rows: Iterable[dict[str, Any]]) -> str:
    listed = [
        f'<li><span class="danger">{_memory_link(row["memory_id"])} '
        f"{_e(row.get('retirement_kind') or 'legacy')}</span>: {_e(row.get('retire_reason'))}</li>"
        for row in rows
    ]
    return f'<ul class="items">{"".join(listed)}</ul>' if listed else "-"


def _review(cur: psycopg.Cursor, query: dict[str, str]) -> str:
    deferred = query.get("deferred") == "1"
    waiting = nominations.pending_nominations(cur, include_deferred=deferred)
    held = nominations.deferred_count(cur)
    toggle = (
        f'<a href="/review">hide the {_e(held)} put off</a>'
        if deferred
        else f'<a href="{_href("/review", deferred="1")}">include the {_e(held)} put off</a>'
    )
    candidate_rows = []
    for row in waiting:
        content = _e(row["content"])
        if row["deferred_at"]:
            content = f'<span class="warn">put off: {_e(row["defer_reason"])}</span>\n\n' + content
        evidence = "".join(
            f"<li>{_e(item['kind'])} {_e(item['created_at'])}<br>what: {_e(item['what'])}"
            f"<br>prevention: {_e(item['prevention'])}</li>"
            for item in row["evidence_rows"]
        )
        candidate_rows.append(
            [
                _td(_short(row["nomination_id"]), "mono"),
                _td(f"{_e(row['kind'])} v{_e(row['version'])}", "nowrap"),
                _td(_e(row["scope_name"])),
                _td(content, "text"),
                _td(f'<ul class="items">{evidence}</ul>' if evidence else "-", "text"),
                _td(_conflicts(row["conflict_rows"]), "text"),
                _td(f"{_e(row['created_at'])}<br>{_e(row['created_by'])}", "nowrap"),
            ]
        )
    candidates = _table(
        ("id", "kind", "scope", "content", "evidence", "retired conflicts", "proposed"),
        candidate_rows,
        empty="no candidates waiting",
    )
    change_rows = []
    for row in memory_changes.pending(cur):
        target = row["target"] or {}
        what = []
        if row["retirement_kind"]:
            what.append(f"retire as {_e(row['retirement_kind'])}: {_e(row['retire_reason'])}")
        if row["restore_reason"]:
            what.append(f"restore: {_e(row['restore_reason'])}")
        what.extend(_e(line) for line in row["delivery_move"])
        if row["successor"]:
            snapshot = row["successor_snapshot"] or {}
            what.append(f"successor: {_e(snapshot.get('content'))}")
            if row["successor_changed"]:
                what.append('<span class="warn">successor changed since proposed</span>')
        if row["destination"]:
            what.append(f"moves to Temporary Context: {_e(row['destination']['content'])}")
        evidence = "".join(
            f"<li>{_e(item.get('kind', 'reference'))} "
            f"{_e(item.get('id') or item.get('ref'))}"
            + (f"<br>{_e(item['observation'])}" if item.get("observation") else "")
            + "</li>"
            for item in row["evidence"] or []
        )
        change_rows.append(
            [
                _td(_short(row["change_id"]), "mono"),
                _td(f"{_e(row['operation'])} v{_e(row['version'])}", "nowrap"),
                _td(
                    f"{_memory_link(row['target_memory_id'])} "
                    f'<span class="muted">{_e(target.get("status"))}</span>\n'
                    f"{_e(target.get('content'))}",
                    "text",
                ),
                _td("\n".join(what) or "-", "text"),
                _td(f'<ul class="items">{evidence}</ul>' if evidence else "-", "text"),
                _td(_conflicts(row["conflicts"]), "text"),
                _td(f"{_e(row['proposed_at'])}<br>{_e(row['proposed_by'])}", "nowrap"),
            ]
        )
    changes = _table(
        ("id", "operation", "target", "change", "evidence", "retired conflicts", "proposed"),
        change_rows,
        empty="no Memory changes waiting",
    )
    return (
        f"<p>{toggle}</p><h2>Candidates</h2>{candidates}"
        f"<h2>Memory changes</h2>{changes}"
        '<p class="muted">Decide these with <span class="mono">mashu review</span> and '
        '<span class="mono">mashu review --changes</span>.</p>'
    )


#: Pages that take only the query string, by path, with their titles.
_LISTS: dict[str, tuple[str, Callable[[psycopg.Cursor, dict[str, str]], str]]] = {
    "/": ("Overview", _overview),
    "/memories": ("Memories", _memory_list),
    "/temporary": ("Temporary Context", _temporary),
    "/tasks": ("Tasks", _task_list),
    "/review": ("Review", _review),
}

#: Pages for one row, by the path segment before its id.
_DETAILS: dict[str, Callable[[psycopg.Cursor, UUID], tuple[str, str]]] = {
    "memories": _memory_detail,
    "tasks": _task_detail,
}


def render(dsn: str | None, path: str, query: dict[str, str]) -> tuple[int, str]:
    """The status and page for one GET, read in a read-only transaction."""
    path = path.rstrip("/") or "/"
    parts = path.strip("/").split("/")
    try:
        if path in _LISTS:
            title, page = _LISTS[path]
            with db.read_only(dsn) as cur:
                return HTTPStatus.OK, _page(title, page(cur, query), here=path)
        if len(parts) == 2 and parts[0] in _DETAILS:
            try:
                row_id = UUID(parts[1])
            except ValueError as error:
                raise NotFound(f"'{parts[1]}' is not an id") from error
            with db.read_only(dsn) as cur:
                title, body = _DETAILS[parts[0]](cur, row_id)
            return HTTPStatus.OK, _page(title, body, here=f"/{parts[0]}")
        raise NotFound(f"no page at {path}")
    except NotFound as missing:
        return HTTPStatus.NOT_FOUND, _page("Not found", f"<p>{_e(missing)}</p>", here="")
    except MashuError as error:
        return HTTPStatus.BAD_REQUEST, _page("Cannot show this", f"<p>{_e(error)}</p>", here="")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, dsn: str | None, port: int) -> None:
        super().__init__((HOST, port), _Handler)
        self.dsn = dsn


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "mashu-gui"

    def do_GET(self) -> None:
        self._answer(with_body=True)

    def do_HEAD(self) -> None:
        self._answer(with_body=False)

    def __getattr__(self, name: str) -> Any:
        # http.server answers a method it has no do_ handler for with 501. Every method other
        # than GET and HEAD is refused here instead, so no verb can reach a page.
        if name.startswith("do_"):
            return self._refuse
        raise AttributeError(name)

    def _refuse(self) -> None:
        self.close_connection = True
        self._send(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "read-only: only GET and HEAD are served\n",
            content_type="text/plain; charset=utf-8",
            extra=(("Allow", "GET, HEAD"),),
        )

    def _known_host(self) -> bool:
        # A page elsewhere can point a name it controls at 127.0.0.1 and read these pages
        # through the browser (DNS rebinding); only the loopback names this server binds are
        # accepted.
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"{HOST}:{port}", f"localhost:{port}")

    def _answer(self, *, with_body: bool) -> None:
        if not self._known_host():
            self._send(HTTPStatus.FORBIDDEN, "unknown host\n", content_type="text/plain")
            return
        split = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(split.query).items()}
        try:
            status, page = render(self.server.dsn, split.path, query)
        except Exception as error:  # noqa: BLE001 - one failed page must not stop the server
            self.log_error("%s", error)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            page = _page("Error", f"<p>{_e(error)}</p>", here="")
        self._send(status, page, with_body=with_body)

    def _send(
        self,
        status: int,
        text: str,
        *,
        content_type: str | None = None,
        extra: Iterable[tuple[str, str]] = (),
        with_body: bool = True,
    ) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        for name, value in _HEADERS:
            if name == "Content-Type" and content_type:
                value = content_type
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if with_body:
            self.wfile.write(data)


def make_server(dsn: str | None, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A server on the loopback address only; port 0 picks a free one."""
    try:
        return _Server(dsn, port)
    except OSError as error:
        raise MashuError(
            f"cannot listen on {HOST}:{port} ({error}); pass another --port"
        ) from error


def serve(dsn: str | None, *, port: int | None = None, open_browser: bool = True) -> int:
    """Serve the pages until interrupted."""
    server = make_server(dsn, DEFAULT_PORT if port is None else port)
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Mashu GUI (read-only) at {url}  Ctrl+C to stop", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
