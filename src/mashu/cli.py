"""Command-line interface for Mashu."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

from mashu import (
    application,
    config,
    db,
    events,
    memories,
    memory_changes,
    nominations,
    projects,
    references,
    routing,
    scopes,
    task_actions,
    task_history,
    tasks,
    temporary,
    tokens,
    topics,
)
from mashu import (
    ledger as ledger_domain,
)
from mashu import (
    migrate as migration,
)
from mashu import (
    traces as trace_domain,
)
from mashu.errors import DuplicateTaskError, MashuError, RefusedError, RetiredConflictError

ACTOR = "user"
GUARD_HOLD = 2
_DAYS = re.compile(r"^(\d+(?:\.\d+)?)(?:d)?$")

#: The tables `show` reaches into, in the order it reports collisions.
_REFERENCE_TABLES = (
    ("memory", "memory_id"),
    ("nomination", "nomination_id"),
    ("ledger", "ledger_id"),
    ("memory_change", "change_id"),
)


class _MashuArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._mashu_arguments: list[str] = []

    def error(self, message: str) -> None:
        is_task_close = any(
            self._mashu_arguments[index : index + 2] == ["task", "close"]
            for index in range(len(self._mashu_arguments) - 1)
        )
        if is_task_close and message.startswith("unrecognized arguments:"):
            message += (
                "\nFor task close, pass --outcome followed by completed, abandoned, or "
                "superseded. Example: mashu task close 1a2b3c4d --outcome completed"
            )
        super().error(message)


def _plain(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _lookup(cur: Any, ref: str, *, table: str, id_col: str, extra_where: str = "") -> list[UUID]:
    """Every id in one table that this reference could be naming."""
    return references.matching_ids(cur, ref, table=table, column=id_col, extra_where=extra_where)


def _resolve(
    cur: Any, ref: str, *, table: str, id_col: str, label: str, extra_where: str = ""
) -> UUID:
    """The one row this reference names, or a refusal saying why it is not one."""
    return references.resolve_id(
        cur,
        ref,
        table=table,
        column=id_col,
        label=label,
        extra_where=extra_where,
    )


def _memory_ref(cur: Any, ref: str) -> UUID:
    return _resolve(cur, ref, table="memory", id_col="memory_id", label="memory")


def _task_ref(cur: Any, ref: str) -> UUID:
    return _resolve(cur, ref, table="task", id_col="task_id", label="task")


def _project_ref(cur: Any, ref: str) -> UUID:
    """A project by the name it was opened under, or by the front of its id."""
    row = projects.get_project(cur, ref)
    if row is not None:
        return row["project_id"]
    if references.is_uuid_ref(ref):
        return _resolve(cur, ref, table="project", id_col="project_id", label="project")
    return projects.require_project(cur, ref)["project_id"]


def _scope(cur: Any, name: str | None) -> UUID | None:
    return scopes.require_scope(cur, name)["scope_id"] if name is not None else None


def _topic(cur: Any, name: str | None) -> UUID | None:
    return topics.require_topic(cur, name)["topic_id"] if name is not None else None


def _delivery_label(row: dict[str, Any]) -> str:
    """How a delivery reads in a listing: topic with its name."""
    if row["delivery"] == "topic":
        return f"topic:{row.get('topic_name') or '-'}"
    return row["delivery"]


def _routed_scope(cur: Any) -> tuple[UUID | None, str | None, bool]:
    return application.routed_scope(cur, os.getcwd())


def _short(value: Any) -> str:
    return str(value)[:8]


def _date(value: Any) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]


def _field(label: str, value: Any) -> None:
    print(f"{label:<10}  {value}")


def _cells(text: str) -> int:
    """How wide a string prints, counting double-width characters as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """Left-justify to a printed width, so a Japanese name keeps its column."""
    return text + " " * max(0, width - _cells(text))


def _flow(text: str, indent: str = "  ", width: int | None = None) -> str:
    """Wrap a long body for reading, counting double-width characters as two."""
    if width is None:
        width = min(88, max(40, shutil.get_terminal_size((88, 24)).columns))
    out: list[str] = []
    for line in text.splitlines() or [""]:
        line = line.strip()
        if not line:
            out.append("")
            continue
        row, used = indent, len(indent)
        for ch in line:
            cost = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            if used + cost > width:
                cut = row.rfind(" ", len(indent) + 1)
                if cut > len(indent):
                    out.append(row[:cut].rstrip())
                    row = indent + row[cut + 1 :]
                else:
                    out.append(row)
                    row = indent
                used = len(indent) + _cells(row[len(indent) :])
                # Drop only the wrap-boundary space; preserve spaces within the text.
                if ch == " " and row == indent:
                    continue
            row += ch
            used += cost
        out.append(row)
    return "\n".join(out)


def _editor_text(content: str) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    command = shlex.split(editor)
    if not command:
        command = ["vi"]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        path = Path(handle.name)
        handle.write(content)
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


def _parse_days(value: str) -> float:
    match = _DAYS.fullmatch(value.strip())
    if match is None:
        raise MashuError("--until expects N, Nd, or N.5d; for example, use --until 1.5d")
    days = float(match.group(1))
    if not 0 < days <= 14:
        raise RefusedError("--until must be greater than 0 and no more than 14 days")
    return days


def _gate_warnings(result: dict[str, Any]) -> None:
    """Say when the entrance check did not run, or ran on a broken list."""
    if result.get("unchecked"):
        print("warning: banned-pattern list unavailable; nothing was checked", file=sys.stderr)
    malformed = result.get("malformed") or 0
    if malformed:
        print(
            f"warning: {malformed} banned-pattern line(s) could not be compiled",
            file=sys.stderr,
        )


def _print_memory_rows(rows: list[dict[str, Any]], *, heading: str = "memories") -> None:
    print(heading)
    for row in rows:
        print(f"{_short(row['memory_id']):8}  {row['content']}")


def _print_state_rows(rows: list[dict[str, Any]]) -> None:
    """The active task cards, each pointing to explicitly fetched detail."""
    print("active task cards")
    for row in rows:
        print(f"{row.get('task_id') or row['task']}  {row['heading']}")
        print(_flow(row["content"], indent="          "))


def cmd_status(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        status = application.status_snapshot(cur)

    if status.schema_pending:
        print(
            f"schema  {len(status.schema_pending)} MIGRATION(S) PENDING: "
            f"{', '.join(status.schema_pending)}"
        )
        print("        writes against the new columns fail until 'mashu admin migrate'")
    else:
        print("schema  up to date")
    active = "  ".join(f"{key}={status.memory_counts.get(key, 0)}" for key in memories.DELIVERIES)
    print(f"active  {active}")
    print(
        f"tokens  always={status.memory_always_tokens}/{config.always_capacity()}  "
        f"worst={status.memory_worst_tokens}/{config.capacity()}"
    )
    print(
        f"cards   active={status.active_states}  "
        f"worst={status.state_worst_tokens}/{config.project_capacity()}"
    )
    heaviest = (
        f"  heaviest={status.topic_heaviest_name} "
        f"{status.topic_heaviest_tokens}/{config.topic_capacity()}"
        if status.topic_heaviest_name
        else ""
    )
    print(f"topics  count={status.topic_count}{heaviest}")
    print(f"temporary  tokens={status.temporary_tokens}/{config.temporary_capacity()}")
    print(f"pending {status.pending_ready + status.pending_deferred}")
    print(f"traces  unexpired={status.traces}")
    print(f"ledger  last_30_days={status.ledger_30d}")
    routes = "  ".join(
        f"{route}={status.delivery_failures_by_route.get(route, 0)}"
        for route in application.DELIVERY_FAILURE_ROUTES
    )
    print(f"delivery  suspected_failures_30d={status.delivery_failures_30d}  {routes}")
    print("scopes")
    print("name                         active  push_tokens")
    for row in status.scope_rows:
        print(f"{row['name'][:28]:28}  {row['n_active']:6}  {row['push_tokens']:11}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        answer = application.bootstrap_preview(cur, cwd=os.getcwd(), actor=ACTOR)
    unapplied = answer.get("schema_pending") or []
    if unapplied:
        print(f"schema    {len(unapplied)} MIGRATION(S) PENDING: {', '.join(unapplied)}")
        print("          writes against the new columns fail until 'mashu admin migrate'")
    routed_text = str(answer.get("routed", False)).lower()
    print(f"scope     {answer.get('scope') or '-'}  routed={routed_text}")
    _print_memory_rows(answer.get("always", []), heading="always")
    _print_memory_rows(answer.get("scoped", []), heading="scoped")
    if answer.get("topics"):
        print("topics")
    for row in answer.get("topics", []):
        print(_flow(row["line"], indent="  "))
    if answer.get("topic_instruction"):
        print(f"          {answer['topic_instruction']}")
    _print_state_rows(answer.get("states", []))
    if answer.get("task_instruction"):
        print(f"          {answer['task_instruction']}")
    print("temporary")
    for row in answer.get("temporary", []):
        print(f"  {row['content']}  (expires {row['expires_at']})")
    print(
        f"tokens    {answer.get('tokens', 0)}/"
        f"{answer.get('capacity', config.total_capacity())}  "
        f"memory={answer.get('memory_tokens', 0)}  "
        f"cards={answer.get('card_tokens', answer.get('project_tokens', 0))}  "
        f"temporary={answer.get('temporary_tokens', 0)}"
    )
    print(f"pending   {answer.get('pending', 0)}")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    if args.until is not None:
        if (
            args.scope is not None
            or args.delivery is not None
            or args.topic is not None
            or args.force
        ):
            raise MashuError(
                "--until cannot be combined with --scope, --delivery, --topic, or --force; "
                "omit those options when recording a temporary condition"
            )
        days = _parse_days(args.until)
        with db.transaction(args.dsn) as cur:
            row = temporary.put_temporary(cur, content=args.body, actor=ACTOR, days=days)
        _gate_warnings(row)
        print(f"temporary  {_short(row['context_id'])}  {row['expires_at']}")
        return 0

    if args.topic is not None and (args.scope is not None or args.delivery not in (None, "topic")):
        raise MashuError(
            "--topic delivers the rule with that topic, which already says where it is read; "
            "omit --scope and --delivery"
        )
    delivery = args.delivery or ("topic" if args.topic else "scope" if args.scope else "always")
    acknowledged: list[UUID] | None = None
    while True:
        try:
            with db.transaction(args.dsn) as cur:
                scope_id = _scope(cur, args.scope)
                row = memories.remember(
                    cur,
                    content=args.body,
                    actor=ACTOR,
                    scope_id=scope_id,
                    delivery=delivery,
                    topic_id=_topic(cur, args.topic),
                    acknowledged_conflicts=acknowledged,
                )
            break
        except RetiredConflictError as conflict:
            if args.force:
                _print_retirement_conflicts(conflict)
                acknowledged = [row["memory_id"] for row in conflict.tombstones]
                continue
            acknowledged = _confirm_override(conflict)
            if acknowledged is None:
                return 1

    _gate_warnings(row)
    if row.get("overrides"):
        print(f"overrode  {len(row['overrides'])} retirement(s)")
    _print_retirement_warnings(row.get("retirement_warnings") or [])
    print(f"remembered  {row['memory_id']}")
    return 0


def _print_retirement_conflicts(conflict: RetiredConflictError) -> None:
    """Show the exact invalidated or legacy conflicts that need acknowledgment."""
    print(str(conflict), file=sys.stderr)
    for row in conflict.tombstones:
        retired = row.get("retired_at")
        when = retired.date().isoformat() if isinstance(retired, datetime) else str(retired or "")
        retirement_kind = row.get("retirement_kind") or "legacy"
        print(
            f"  retired {_short(row['memory_id'])}  {retirement_kind}  {when}",
            file=sys.stderr,
        )
        print(f"    reason: {row['retire_reason']}", file=sys.stderr)
        if row.get("superseded_by"):
            print(f"    successor: {row['superseded_by']}", file=sys.stderr)
        if row.get("relocated_to_id"):
            print(
                f"    moved to {row['relocated_to_kind']}: {row['relocated_to_id']}",
                file=sys.stderr,
            )


def _confirm_override(conflict: RetiredConflictError) -> list[UUID] | None:
    """Show the reasons and treat an affirmative direct action as acknowledgment."""
    _print_retirement_conflicts(conflict)
    if not sys.stdin.isatty():
        print(
            "refusing to write it back without a direct acknowledgment; review the reasons and "
            "retry with --force",
            file=sys.stderr,
        )
        return None
    try:
        answer = (
            input("write it back after reading these retirement reasons? [y/N] ").strip().lower()
        )
    except (EOFError, KeyboardInterrupt):
        return None
    return [row["memory_id"] for row in conflict.tombstones] if answer in ("y", "yes") else None


def _print_retirement_warnings(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        kind = row.get("retirement_kind") or "legacy"
        print(f"warning  retired {kind}  {_short(row['memory_id'])}")
        print(f"  reason: {row['retire_reason']}")
        if row.get("superseded_by"):
            print(f"  successor: {row['superseded_by']}")
        if row.get("relocated_to_id"):
            print(f"  moved to {row['relocated_to_kind']}: {row['relocated_to_id']}")


def cmd_retire(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        memory_id = _memory_ref(cur, args.memory_id)
        row = memories.retire(
            cur,
            memory_id,
            reason=args.reason,
            actor=ACTOR,
            retirement_kind=args.kind,
            superseded_by=(
                _resolve(
                    cur,
                    args.superseded_by,
                    table="memory",
                    id_col="memory_id",
                    label="successor memory",
                )
                if args.superseded_by
                else None
            ),
            relocated_to_kind="temporary_context" if args.temporary_context else None,
            relocated_to_id=(
                _resolve(
                    cur,
                    args.temporary_context,
                    table="temporary_context",
                    id_col="context_id",
                    label="temporary context",
                )
                if args.temporary_context
                else None
            ),
        )
    _gate_warnings(row)
    print(f"retired  {row['memory_id']}")
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    content = args.content
    with db.transaction(args.dsn) as cur:
        memory_id = _memory_ref(cur, args.memory_id)
        current = memories.get_memory(cur, memory_id)
    if current is None:
        raise MashuError(f"memory '{memory_id}' not found")
    if content is None:
        content = _editor_text(current["content"])
    with db.transaction(args.dsn) as cur:
        row = memories.revise(cur, memory_id, content=content, actor=ACTOR)
    print(f"revised  {row['memory_id']}")
    return 0


def cmd_pain(args: argparse.Namespace) -> int:
    if args.task and args.prevention_kind != "work":
        raise MashuError(
            "--task can only be used with --prevention-kind work. Add --prevention-kind work "
            "to file one-time work on a task, or remove --task for a rule."
        )
    with db.transaction(args.dsn) as cur:
        row = ledger_domain.report_pain(
            cur,
            kind=args.kind,
            what=args.what,
            prevention=args.prevention,
            actor=ACTOR,
            scope_id=_scope(cur, args.scope),
            prevention_kind=args.prevention_kind,
            task_id=_task_ref(cur, args.task) if args.task else None,
        )
    _gate_warnings(row)
    matches = row.get("matches", {})
    print(f"pain  {_short(row['ledger_id'])}")
    for name in ("ledger", "traces", "tombstones", "memories"):
        print(f"{name}: {len(matches.get(name, []))}")
    if row.get("prevention_kind") == "work":
        filed = row.get("filed_task")
        print(
            f"prevention  work, filed on task {_short(filed)}"
            if filed
            else "prevention  work, filed nowhere"
        )
        if row.get("note"):
            print(_flow(row["note"], indent="    "))
    elif row.get("tombstone_suppressed"):
        print("nomination  withheld: retired knowledge already covers this")
        for stone in matches.get("tombstones", []):
            print(f"  retired {_short(stone['memory_id'])}: {stone['retire_reason']}")
    elif row.get("delivery_suspect"):
        print("nomination  withheld: this rule is already active; suspect the delivery")
        for held in matches.get("memories", []):
            print(f"  active {_short(held['memory_id'])} [{held['delivery']}]: {held['content']}")
    elif row.get("nomination_existing"):
        nomination = row.get("nomination") or {}
        held = len(nomination.get("evidence") or [])
        print(f"nomination  existing {_short(nomination.get('nomination_id'))}, {held} evidence")
    elif row.get("nomination"):
        print(f"nomination  created {_short(row['nomination']['nomination_id'])}")
    else:
        print("pain  first pain recorded")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        rows = ledger_domain.ledger_entries(
            cur,
            limit=args.limit,
            scope_id=_scope(cur, args.scope),
        )
    print("id        kind       date        what")
    for row in rows:
        print(
            f"{_short(row['ledger_id']):8}  {row['kind']:<9}  "
            f"{_date(row.get('created_at'))}  {row['what']}"
        )
        # Include the prevention text used for matching.
        label = "prevention"
        if row.get("prevention_kind") == "work":
            # Show whether this work fix was filed on a task.
            where = f"filed {_short(row['filed_task'])}" if row.get("filed_task") else "UNFILED"
            label = f"prevention  (work, {where})"
        print(f"    {label}  {row['prevention']}")
    return 0


def _ledger_rows(cur: Any, ids: list[UUID]) -> list[dict[str, Any]]:
    """The named ledger rows, in the order they were cited."""
    if not ids:
        return []
    cur.execute(
        """
        SELECT l.*, s.name AS scope_name
        FROM ledger l LEFT JOIN scope s ON s.scope_id = l.scope_id
        WHERE l.ledger_id = ANY(%s)
        """,
        (list(ids),),
    )
    by_id = {row["ledger_id"]: row for row in cur.fetchall()}
    return [by_id[item] for item in ids if item in by_id]


def _print_evidence(cur: Any, evidence: list[UUID] | None) -> None:
    """The pains a row rests on, opened out."""
    print("evidence")
    for row in _ledger_rows(cur, list(evidence or [])):
        print(
            f"  {_short(row['ledger_id'])}  {row['kind']}  "
            f"{_date(row['created_at'])}  {row['what']}"
        )
        print(f"    prevention  {row['prevention']}")
        print(f"    source      {row['source'] or '-'}")


def _show_memory(cur: Any, memory_id: UUID) -> None:
    cur.execute(
        """
        SELECT m.*, s.name AS scope_name, t.name AS topic_name, t.trigger AS topic_trigger
        FROM memory m
        LEFT JOIN scope s ON s.scope_id = m.scope_id
        LEFT JOIN topic t ON t.topic_id = m.topic_id
        WHERE m.memory_id = %s
        """,
        (memory_id,),
    )
    row = cur.fetchone()
    _field("memory", row["memory_id"])
    if row["status"] == "retired":
        _field("status", f"retired  {row['retire_reason']}")
    else:
        _field("status", row["status"])
    _field("delivery", _delivery_label(row))
    if row["delivery"] == "topic":
        _field("trigger", row["topic_trigger"])
    _field("scope", row["scope_name"] or "-")
    _field("created", f"{_date(row['created_at'])}  {row['created_by']}")
    _field("tokens", tokens.pushed_cost([row["content"]]))
    print("content")
    print(f"  {row['content']}")
    _print_evidence(cur, row["evidence"])
    cur.execute(
        """
        SELECT content, created_at FROM memory_revision
        WHERE memory_id = %s ORDER BY created_at, revision_id
        """,
        (memory_id,),
    )
    print("revisions")
    for revision in cur.fetchall():
        print(f"  {_date(revision['created_at'])}  {revision['content']}")
    if row["status"] == "retired":
        _field("retirement kind", row.get("retirement_kind") or "legacy")
        _field("successor", row.get("superseded_by") or "-")
        if row.get("relocated_to_id"):
            _field(
                "moved to",
                f"{row['relocated_to_kind']}  {row['relocated_to_id']}",
            )
    cur.execute(
        """
        SELECT event_type, actor, detail, created_at
        FROM event_log
        WHERE memory_id = %s
          AND event_type IN ('memory_retired', 'memory_restored', 'memory_retirement_classified')
        ORDER BY event_id
        """,
        (memory_id,),
    )
    history = cur.fetchall()
    if history:
        print("retirement history")
        for event in history:
            _field("event", f"{event['event_type']}  {_date(event['created_at'])}")
            _field("actor", event["actor"])
            detail = event.get("detail") or {}
            if detail.get("approval_source"):
                _field("approval", detail["approval_source"])


def _show_memory_change(cur: Any, change_id: UUID) -> None:
    row = memory_changes.get(cur, change_id)
    if row is None:
        raise MashuError(f"no memory change {change_id}")
    _field("memory change", row["change_id"])
    _field("operation", row["operation"])
    _field("status", row["status"])
    _field("version", row["version"])
    _field("proposed", f"{row['proposed_by']}  {_date(row['proposed_at'])}")
    _field("target", f"{row['target_memory_id']}  revision {row['target_revision_id']}")
    print("target body")
    print(f"  {row['target']['content']}")
    if row["operation"] in ("retire", "replace"):
        _field("retirement kind", row["retirement_kind"])
        _field("retire reason", row["retire_reason"])
    if row["operation"] == "restore":
        _field("restore reason", row["restore_reason"])
    if row["operation"] == "redeliver":
        for line in row["delivery_move"]:
            print(f"  {line}")
    if row.get("successor"):
        _field("successor nomination", row["successor_nomination_id"])
        _field("successor version", row["successor_snapshot"]["version"])
        print(f"  proposed  {row['successor_snapshot']['content']}")
        _field("candidate scope", row["successor_snapshot"]["scope_id"] or "-")
        _field("candidate kind", row["successor_snapshot"]["kind"])
        _field("candidate evidence", ", ".join(row["successor_snapshot"]["evidence"]))
        for line in row["delivery_move"]:
            print(f"  {line}")
        if row.get("successor_changed"):
            print(f"  current v{row['successor']['version']}  {row['successor']['content']}")
            _field("current candidate scope", row["successor"]["scope_id"] or "-")
            _field("candidate status", "changed since proposal; refresh and reread")
    if row.get("relocated_to_id"):
        _field("destination", f"{row['relocated_to_kind']}  {row['relocated_to_id']}")
    if row["status"] == "applied":
        _field("applied by", row["decided_by"] or "-")
        _field("approval", row["approval_source"] or "-")
        _field("request", row["apply_request_id"] or "-")
    print("evidence")
    for evidence in row.get("evidence", []):
        reference = evidence.get("id") or evidence.get("ref") or "-"
        print(f"  {evidence['kind']}  {reference}")
        if evidence.get("observation"):
            print(f"    {evidence['observation']}")
    print("retirement conflicts")
    for conflict in row.get("conflicts", []):
        print(
            f"  {conflict['memory_id']}  {conflict.get('retirement_kind') or 'legacy'}  "
            f"{conflict['retire_reason']}"
        )


def _show_nomination(cur: Any, nomination_id: UUID) -> None:
    cur.execute(
        """
        SELECT n.*, s.name AS scope_name
        FROM nomination n LEFT JOIN scope s ON s.scope_id = n.scope_id
        WHERE n.nomination_id = %s
        """,
        (nomination_id,),
    )
    row = cur.fetchone()
    _field("nomination", row["nomination_id"])
    _field("kind", row["kind"])
    _field("status", row["status"])
    _field("version", row["version"])
    _field("scope", row["scope_name"] or "-")
    print("content")
    print(f"  {row['content']}")
    # Show retirement conflicts before the evidence list.
    for conflict in nominations.conflict_rows(cur, row["conflicts"]):
        print(
            f"! contradicts retired {_short(conflict['memory_id'])}  "
            f"{conflict.get('retirement_kind') or 'legacy'}"
        )
        print(f"  retired because: {conflict['retire_reason']}")
        if conflict.get("superseded_by"):
            print(f"  successor: {conflict['superseded_by']}")
        if conflict.get("relocated_to_id"):
            print(f"  moved to {conflict['relocated_to_kind']}: {conflict['relocated_to_id']}")
    _print_evidence(cur, row["evidence"])
    if row["status"] == "admitted":
        _field("executed by", row["decided_by"] or "-")
        _field("approval", row["approval_source"] or "-")
        _field("request", row["admit_request_id"] or "-")
    if row["status"] == "declined":
        _field("declined", row["decision_reason"] or "-")


def _show_ledger(cur: Any, ledger_id: UUID) -> None:
    row = _ledger_rows(cur, [ledger_id])[0]
    _field("ledger", row["ledger_id"])
    _field("kind", row["kind"])
    _field("created", f"{_date(row['created_at'])}  {row['created_by']}")
    _field("scope", row["scope_name"] or "-")
    _field("what", row["what"])
    _field("prevention", row["prevention"])
    _field("source", row["source"] or "-")
    # Related nominations and memories.
    cur.execute(
        """
        SELECT 'nomination' AS held_in, nomination_id AS row_id, status, created_at
        FROM nomination WHERE %(ledger)s::uuid = ANY(evidence)
        UNION ALL
        SELECT 'memory', memory_id, status, created_at
        FROM memory WHERE %(ledger)s::uuid = ANY(evidence)
        ORDER BY created_at, row_id
        """,
        {"ledger": ledger_id},
    )
    print("cited by")
    for cite in cur.fetchall():
        print(f"  {cite['held_in']:<10}  {_short(cite['row_id'])}  {cite['status']}")


_SHOW = {
    "memory": _show_memory,
    "nomination": _show_nomination,
    "ledger": _show_ledger,
    "memory_change": _show_memory_change,
}


def cmd_show(args: argparse.Namespace) -> int:
    """One row, whichever of the three tables it lives in, opened in full."""
    with db.transaction(args.dsn) as cur:
        found = [
            (table, value)
            for table, id_col in _REFERENCE_TABLES
            for value in _lookup(cur, args.ref, table=table, id_col=id_col)
        ]
        if not found:
            raise MashuError(
                f"no memory, nomination, ledger row, or Memory change matches '{args.ref}'; "
                "check the ID or try a longer prefix"
            )
        if len(found) > 1:
            listed = "\n".join(f"  {table}  {_short(value)}" for table, value in found)
            raise MashuError(
                f"'{args.ref}' names more than one row:\n{listed}\n"
                "Use a longer ID prefix to select one row."
            )
        table, value = found[0]
        _SHOW[table](cur, value)
    return 0


def cmd_memories(args: argparse.Namespace) -> int:
    status = "retired" if args.retired else "active"
    with db.transaction(args.dsn) as cur:
        scope_id = _scope(cur, args.scope) if args.scope else None
        cur.execute(
            """
            SELECT m.*, s.name AS scope_name, t.name AS topic_name
            FROM memory m
            LEFT JOIN scope s ON s.scope_id = m.scope_id
            LEFT JOIN topic t ON t.topic_id = m.topic_id
            WHERE m.status = %(status)s
              AND (%(scope)s::uuid IS NULL OR m.scope_id = %(scope)s::uuid)
            ORDER BY m.delivery, s.name, t.name, m.created_at
            """,
            {"status": status, "scope": scope_id},
        )
        rows = cur.fetchall()
    print("id        delivery              scope                 tokens  content")
    for row in rows:
        print(
            f"{_short(row['memory_id']):8}  {_pad(_delivery_label(row)[:20], 20)}  "
            f"{(row['scope_name'] or '-')[:20]:20}  "
            f"{tokens.pushed_cost([row['content']]):6}  {row['content']}"
        )
        if status == "retired":
            print(f"    retired  {row['retire_reason']}")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        rows = trace_domain.search_traces(
            cur,
            query=args.query,
            scope_id=_scope(cur, args.scope),
        )
    if not rows:
        print("nothing matches" if args.query else "no unexpired traces")
        return 0
    for number, row in enumerate(rows):
        if number:
            print()
        head = (
            f"{_short(row['trace_id'])}  {_date(row['created_at'])}  "
            f"{row.get('scope_name') or '-'}  until {_date(row['expires_at'])}"
        )
        if row.get("score") is not None:
            head += f"  match {row['score']:.2f}"
        print(head)
        print(_flow(row["content"]))
    return 0


def _pending_by_id(cur: Any, value: str) -> dict[str, Any]:
    wanted = _resolve(
        cur,
        value,
        table="nomination",
        id_col="nomination_id",
        label="pending nomination",
        extra_where="status = 'pending'",
    )
    for row in nominations.pending_nominations(cur):
        if row["nomination_id"] == wanted:
            return row
    raise MashuError(f"pending nomination '{value}' not found")


def _print_pending(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("nothing waiting for review")
    for row in rows:
        print(
            f"{row['nomination_id']}  v{row['version']}  {row['kind']}  "
            f"{row.get('scope_name') or '-'}"
        )
        print(f"  {row['content']}")
        if row.get("deferred_at"):
            print(f"  deferred: {row.get('defer_reason') or ''}")
        # Above the evidence here too.
        for conflict in row.get("conflict_rows", []):
            print(
                f"  ! retired conflict {_short(conflict['memory_id'])}  "
                f"{conflict.get('retirement_kind') or 'legacy'}"
            )
            print(f"    retired because: {conflict['retire_reason']}")
            if conflict.get("superseded_by"):
                print(f"    successor: {conflict['superseded_by']}")
            if conflict.get("relocated_to_id"):
                print(
                    f"    moved to {conflict['relocated_to_kind']}: {conflict['relocated_to_id']}"
                )
        for evidence in row.get("evidence_rows", []):
            print(
                f"  evidence {evidence['kind']} {evidence.get('created_at', '')}: "
                f"{evidence['what']} / {evidence['prevention']}"
            )


def _pending_change_by_ref(cur: Any, value: str) -> dict[str, Any]:
    wanted = _resolve(
        cur,
        value,
        table="memory_change",
        id_col="change_id",
        label="pending memory change",
        extra_where="status = 'pending'",
    )
    row = memory_changes.get(cur, wanted)
    if row is None or row["status"] != "pending":
        raise MashuError(f"pending memory change '{value}' not found")
    return row


def _print_memory_changes(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("nothing waiting for Memory change review")
    for row in rows:
        print(
            f"{row['change_id']}  {row['operation']}  v{row['version']}  "
            f"{row.get('retirement_kind') or '-'}  target {row['target_memory_id']} "
            f"revision {row['target_revision_id']}  proposed by {row['proposed_by']} "
            f"{_date(row['proposed_at'])}"
        )
        print(f"  {row['target']['content']}")
        if row["operation"] in ("retire", "replace"):
            print(f"  retirement reason: {row['retire_reason']}")
        if row["operation"] == "restore":
            print(f"  restore reason: {row['restore_reason']}")
        if row.get("successor"):
            print(f"  successor: {row['successor']['content']}")
        for line in row["delivery_move"]:
            print(f"  {line}")
        for conflict in row.get("conflicts", []):
            print(
                f"  ! {conflict.get('retirement_kind') or 'legacy'} conflict "
                f"{conflict['memory_id']}: {conflict['retire_reason']}"
            )
        for evidence in row.get("evidence", []):
            print(
                f"  evidence {evidence['kind']}: {evidence.get('id') or evidence.get('ref') or '-'}"
            )


def cmd_review(args: argparse.Namespace) -> int:
    if args.changes and (
        args.admit
        or args.decline
        or args.apply_change
        or args.decline_change
        or args.withdraw_change
    ):
        raise MashuError(
            "--changes opens the Memory change queue by itself. Remove --changes to make an "
            "admission or change decision, or remove the decision option to review changes."
        )
    with db.transaction(args.dsn) as cur:
        pending_schema = migration.pending(cur)
    if pending_schema:
        raise MashuError(
            f"store is behind the code: {', '.join(pending_schema)} not applied; "
            "run 'mashu admin migrate'"
        )
    if args.apply_change:
        if args.version is None:
            raise MashuError(
                "--apply-change requires --version from the proposal you reviewed; "
                "get it with 'mashu review --changes --list'. Example: "
                f"mashu review --apply-change {args.apply_change} --version <version>"
            )
        with db.transaction(args.dsn) as cur:
            change_id = _resolve(
                cur,
                args.apply_change,
                table="memory_change",
                id_col="change_id",
                label="memory change",
            )
            change = memory_changes.get(cur, change_id)
            request_id = args.request_id or uuid4()
            result = memory_changes.apply(
                cur,
                change_id,
                version=args.version,
                request_id=request_id,
                approval={
                    "kind": "user_direct",
                    "conflict_ids": args.ack_conflict or [],
                },
                actor=ACTOR,
            )
        target = result.get("memory") or {}
        suffix = f" → {target['memory_id']}" if result["operation"] == "replace" else ""
        print(f"{result['operation']}  {change['target_memory_id']}{suffix}\nrequest  {request_id}")
        return 0
    if args.decline_change or args.withdraw_change:
        if not args.reason:
            decision = "--decline-change" if args.decline_change else "--withdraw-change"
            reference = args.decline_change or args.withdraw_change
            raise MashuError(
                f"{decision} requires --reason. Example: mashu review {decision} {reference} "
                '--reason "no longer needed"'
            )
        status = "declined" if args.decline_change else "withdrawn"
        reference = args.decline_change or args.withdraw_change
        with db.transaction(args.dsn) as cur:
            change = _pending_change_by_ref(cur, reference)
            memory_changes.decide(
                cur,
                change["change_id"],
                status=status,
                actor=ACTOR,
                reason=args.reason,
            )
        print(f"{status}  {change['change_id']}")
        return 0
    if args.list:
        with db.transaction(args.dsn) as cur:
            if args.changes:
                rows = memory_changes.pending(cur)
            else:
                rows = nominations.pending_nominations(cur, include_deferred=args.all)
        _print_memory_changes(rows) if args.changes else _print_pending(rows)
        return 0
    if args.changes:
        from mashu import memory_change_ui

        return memory_change_ui.run(args.dsn)
    if args.admit:
        if args.version is None:
            raise MashuError(
                "--admit requires --version from the candidate you reviewed; "
                "get it with 'mashu review --list'. Example: "
                f"mashu review --admit {args.admit} --version <version>"
            )
        with db.transaction(args.dsn) as cur:
            nomination_id = _resolve(
                cur,
                args.admit,
                table="nomination",
                id_col="nomination_id",
                label="nomination",
            )
            cur.execute("SELECT * FROM nomination WHERE nomination_id = %s", (nomination_id,))
            nomination = cur.fetchone()
            scope_id = _scope(cur, args.scope) if args.scope else None
            delivery = args.delivery or (
                "topic"
                if args.topic
                else "scope"
                if (args.scope or nomination.get("scope_id"))
                else "always"
            )
            request_id = args.request_id or uuid4()
            row = nominations.admit(
                cur,
                nomination["nomination_id"],
                actor=ACTOR,
                delivery=delivery,
                expected_version=args.version,
                scope_id=scope_id,
                topic_id=_topic(cur, args.topic),
                approval={"kind": "user_direct", "conflict_ids": args.ack_conflict or []},
                request_id=request_id,
            )
        print(f"admitted  {row['memory_id']}\nrequest  {request_id}")
        return 0
    if args.decline:
        if not args.reason:
            raise MashuError(
                "--decline requires --reason. Example: "
                f'mashu review --decline {args.decline} --reason "outdated"'
            )
        with db.transaction(args.dsn) as cur:
            nomination = _pending_by_id(cur, args.decline)
            row = nominations.decline(
                cur, nomination["nomination_id"], actor=ACTOR, reason=args.reason
            )
        _gate_warnings(row)
        print(f"declined  {args.decline}")
        return 0

    from mashu import review_ui

    return review_ui.run(args.dsn, show_deferred=args.all)


def cmd_deliver(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = memories.set_delivery(
            cur,
            _memory_ref(cur, args.memory_id),
            delivery=args.delivery,
            actor=ACTOR,
            scope_id=_scope(cur, args.scope) if args.scope else None,
            clear_scope=args.no_scope,
            topic_id=_topic(cur, args.topic),
        )
    print(f"delivery  {row['memory_id']}  {row['delivery']}")
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        scope_id, _, _ = _routed_scope(cur)
        topic, rules = topics.action_rules(cur, args.action, scope_id)
        if rules:
            events.record(
                cur,
                "guard_served",
                ACTOR,
                detail={
                    "action": args.action,
                    "count": len(rules),
                    "topic_id": str(topic["topic_id"]),
                },
            )

    if not rules:
        if args.json:
            print("[]")
        return 0
    pinned = [
        {"memory_id": row["memory_id"], "content": row["content"], "topic": topic["name"]}
        for row in rules
    ]
    if args.json:
        print(json.dumps(_plain(pinned), ensure_ascii=False))
    else:
        for row in pinned:
            print(row["content"])
    return GUARD_HOLD


def cmd_scope(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        if args.add:
            row = scopes.create_scope(cur, name=args.add, summary=args.about, actor=ACTOR)
        else:
            rows = scopes.list_scopes(cur)
    if args.add:
        _gate_warnings(row)
        print(f"created  {row['name']}")
        return 0
    print("name                         active  push_tokens")
    for row in rows:
        print(f"{row['name'][:28]:28}  {row['n_active']:6}  {row['push_tokens']:11}")
    return 0


def _print_topic_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no topics")
        return
    width = max(_cells("name"), *(_cells(row["name"]) for row in rows))
    print(f"{_pad('name', width)}  {'scope':<20}  rules  body  trigger")
    for row in rows:
        before = f"  (before: {row['action']})" if row.get("action") else ""
        print(
            f"{_pad(row['name'], width)}  {(row['scope_name'] or 'every session')[:20]:20}  "
            f"{row['rules']:5}  {row['body_tokens']:4}  {row['trigger']}{before}"
        )


def cmd_topic(args: argparse.Namespace) -> int:
    if args.topic_command == "show":
        if any(flag is not None for flag in (args.add, args.edit, args.remove)):
            raise MashuError("topic show reads one topic; run --add, --edit, or --remove alone")
        with db.transaction(args.dsn) as cur:
            answer = topics.topic_rules(cur, args.name)
        topic = answer["topic"]
        _field("topic", topic["name"])
        _field("scope", topic["scope"] or "every session")
        _field("trigger", topic["trigger"])
        if topic["action"]:
            _field("before", topic["action"])
        print("rules")
        for row in answer["memories"]:
            print(f"  {_short(row['memory_id'])}  {row['content']}")
        return 0
    if args.add is None and args.edit is None and (args.trigger or args.scope or args.rename):
        raise MashuError("--trigger and --scope go with --add or --edit; --rename with --edit")
    if args.edit is None and (args.rename is not None or args.action or args.no_action):
        raise MashuError("--rename, --action, and --no-action go with --edit")
    with db.transaction(args.dsn) as cur:
        if args.add is not None:
            if args.trigger is None:
                raise MashuError(
                    "--add requires --trigger: one sentence saying when to read the topic. "
                    'Example: mashu topic --add audio-trim --trigger "Before trimming audio"'
                )
            if args.scope == "-":
                raise MashuError("omit --scope to list the topic in every session")
            row = topics.create_topic(
                cur,
                name=args.add,
                trigger=args.trigger,
                scope_id=_scope(cur, args.scope),
                actor=ACTOR,
            )
            print(f"created  {row['name']}")
            return 0
        if args.edit is not None:
            current = topics.require_topic(cur, args.edit)
            row = topics.update_topic(
                cur,
                current["topic_id"],
                actor=ACTOR,
                name=args.rename,
                trigger=args.trigger,
                scope_id=_scope(cur, args.scope) if args.scope not in (None, "-") else None,
                clear_scope=args.scope == "-",
                action=args.action,
                clear_action=args.no_action,
            )
            print(f"edited  {row['name']}")
            return 0
        if args.remove is not None:
            current = topics.require_topic(cur, args.remove)
            removed = topics.remove_topic(cur, current["topic_id"], actor=ACTOR)
            print(f"{removed['removed']}  {current['name']}")
            return 0
        rows = topics.list_topics(cur)
    _print_topic_rows(rows)
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        if args.add:
            if args.scope is None:
                raise MashuError(
                    "--add requires --scope. Example: mashu route --add <path> --scope <name>"
                )
            row = application.set_route(
                cur, path_prefix=args.add, scope_name=args.scope, actor=ACTOR
            )
            print(f"route  {row['path_prefix']}  {args.scope}")
            return 0
        if args.ignore:
            if args.scope is not None:
                raise MashuError(
                    "--ignore cannot be combined with --scope; ignored paths are unscoped"
                )
            row = application.ignore_route(cur, path_prefix=args.ignore, actor=ACTOR)
            print(f"ignored  {row['path_prefix']}")
            return 0
        if args.remove:
            if args.scope is not None:
                raise MashuError(
                    "--remove cannot be combined with --scope; omit --scope to remove the route"
                )
            removed = application.remove_route(cur, path_prefix=args.remove, actor=ACTOR)
            print("removed" if removed else "not found")
            return 0
        rows = routing.all_routes(cur)
    # Size this column by display width so full routes remain distinguishable.
    header = "path_prefix"
    width = max([_cells(header), *(_cells(row["path_prefix"]) for row in rows)])
    print(f"{header}{' ' * (width - _cells(header))}  scope")
    for row in rows:
        prefix = row["path_prefix"]
        pad = " " * (width - _cells(prefix))
        print(f"{prefix}{pad}  {row.get('scope_name') or '(ignored)'}")
    return 0


def _print_task_rows(rows: list[dict[str, Any]], *, project: bool = True) -> None:
    """Each task named on its own line, with its state under its heading."""
    for row in rows:
        task, state = row["task"], row["state"]
        head = f"{_short(task['task_id']):8}  {task['name']}"
        if project:
            head += f"  ({task['project_name']})"
        print(head)
        summary = state["status_text"] or state["goal"] or ""
        print(_flow(f"{row['heading']}  {summary}".rstrip(), indent="          "))
        proposal = row.get("proposal")
        if proposal:
            # A third line only where there is one.
            said = f"proposed {proposal['outcome']} on {proposal['on_date']}"
            if proposal["stale"]:
                said += ", state written since"
            print(_flow(f"{said}: {proposal['reason']}", indent="          "))


def cmd_project_list(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        rows = projects.list_projects(cur)
    width = max([_cells("name"), *(_cells(row["name"]) for row in rows)])
    print(f"{_pad('name', width)}  scope                 active  dormant  closed")
    for row in rows:
        print(
            f"{_pad(row['name'], width)}  {(row['scope_name'] or '-')[:20]:20}  "
            f"{row['n_active']:6}  {row['n_dormant']:7}  {row['n_closed']:6}"
        )
    return 0


def cmd_project_create(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = projects.create_project(
            cur,
            name=args.name,
            actor=ACTOR,
            scope_id=_scope(cur, args.scope) if args.scope else None,
        )
    print(f"created  {_short(row['project_id'])}  {row['name']}")
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = projects.show_project(cur, _project_ref(cur, args.ref))
        open_tasks = tasks.task_list(cur, project=row["project_id"], activity="open")
        _field("project", row["project_id"])
        _field("name", row["name"])
        _field("scope", row["scope_name"] or "-")
        _field("created", _date(row["created_at"]))
        if row["archived_at"] is not None:
            _field("archived", _date(row["archived_at"]))
        _field(
            "tasks",
            f"active={row['n_active']}  dormant={row['n_dormant']}  closed={row['n_closed']}",
        )
        print("open tasks")
        _print_task_rows(open_tasks, project=False)
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    """The tasks in one activity, active unless another set is asked for."""
    activity = "dormant" if args.dormant else "closed" if args.closed else "active"
    with db.transaction(args.dsn) as cur:
        project_id = _project_ref(cur, args.project) if args.project else None
        rows = tasks.task_list(cur, project=project_id, activity=activity)
    if not rows:
        print(f"no {activity} tasks")
        return 0
    print(f"{activity} tasks")
    _print_task_rows(rows)
    return 0


def _print_state_fields(state: dict[str, Any]) -> None:
    for label, field in (("goal", "goal"), ("approach", "approach"), ("status", "status_text")):
        if state.get(field):
            print(label)
            print(_flow(state[field]))
    for field in tasks.LIST_FIELDS:
        items = state.get(field) or []
        if not items:
            continue
        print(field.replace("_", " "))
        for item in items:
            print(_flow(f"- {item}"))


def cmd_task_show(args: argparse.Namespace) -> int:
    """One task in full: what it is now, and the history that got it there."""
    with db.transaction(args.dsn) as cur:
        row = task_history.expanded_task(
            cur,
            _task_ref(cur, args.ref),
            attempts=True,
            decisions=True,
            artifacts=True,
            checkpoints=True,
        )
    task, state = row["task"], row["state"]
    _field("task", task["task_id"])
    _field("name", task["name"])
    _field("project", task["project_name"])
    if task["status"] == "closed":
        closed = f"closed  {task['outcome']}"
        if task["close_reason"]:
            closed += f": {task['close_reason']}"
        _field("status", closed)
    else:
        _field("status", f"open  {row['activity']}  (lease to {_date(task['active_until'])})")
    _field("created", f"{_date(task['created_at'])}  {task['created_by']}")
    print(f"{row['heading']}  {state['updated_by']}")
    _print_state_fields(state)

    # Print artifact locators in full; the store keeps references, not file bodies.
    artifacts = {artifact["reference_id"]: artifact for artifact in row["artifacts"]}
    print("artifacts")
    for artifact in row["artifacts"]:
        label = f"  {artifact['label']}" if artifact["label"] else ""
        print(
            f"  {_short(artifact['reference_id'])}  {_date(artifact['created_at'])}  "
            f"{artifact['kind']:<10}  {artifact['locator']}{label}"
        )
    print("checkpoints")
    for point in row["checkpoints"]:
        print(f"  {_date(point['created_at'])}  {point['created_by']}  {point['what_changed']}")
        for reference_id in point["evidence"]:
            cited = artifacts.get(reference_id)
            if cited is None:
                print(f"    evidence  {_short(reference_id)}")
            else:
                print(f"    evidence  {cited['kind']}  {cited['locator']}")
    print("attempts")
    for attempt in row["attempts"]:
        print(f"  {_date(attempt['created_at'])}  {attempt['created_by']}  {attempt['attempt']}")
        for label in ("result", "reason", "next"):
            if attempt[label]:
                print(f"    {label:<8}  {attempt[label]}")
    print("decisions")
    for decision in row["decisions"]:
        print(
            f"  {_date(decision['created_at'])}  {decision['created_by']}  {decision['decision']}"
        )
        if decision["reason"]:
            print(f"    reason      {decision['reason']}")
        if decision["supersedes_id"]:
            print(f"    supersedes  {_short(decision['supersedes_id'])}")
    return 0


def _task_project(cur: Any, given: str | None) -> UUID:
    """The project named, or the one this working directory already belongs to."""
    return task_actions.resolve_task_project(cur, given, cwd=os.getcwd())


def cmd_task_create(args: argparse.Namespace) -> int:
    """Open a task, unless one that reads like it is already open (v3 5.2)."""
    try:
        with db.transaction(args.dsn) as cur:
            row = tasks.task_create(
                cur,
                project=_task_project(cur, args.project),
                name=args.name,
                actor=ACTOR,
                goal=args.goal,
                force=args.force,
            )
    except DuplicateTaskError as clash:
        # Show candidate ids so the caller can continue an existing task.
        print(str(clash), file=sys.stderr)
        for candidate in clash.candidates:
            found = candidate["task"]
            print(
                f"  {_short(found['task_id'])}  {candidate['heading']}  {found['name']}",
                file=sys.stderr,
            )
        print(
            "add --force to this mashu task create command only if you intend to open a "
            "second task",
            file=sys.stderr,
        )
        return 1
    _gate_warnings(row)
    print(f"task  {_short(row['task']['task_id'])}  {row['task']['name']}")
    return 0


def cmd_task_touch(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        task_id = _task_ref(cur, args.ref)
        row = tasks.touch(cur, task_id, actor=ACTOR)
    print(f"touched  {_short(task_id)}  active to {_date(row['task']['active_until'])}")
    return 0


def cmd_task_close(args: argparse.Namespace) -> int:
    """End a task. Only a person reaches this, and only with an outcome (v3 6)."""
    if not args.ref:
        from mashu import close_ui

        return close_ui.run(args.dsn, project=args.project)
    if not args.outcome:
        raise MashuError(
            f"Choose an outcome with --outcome: {', '.join(tasks.OUTCOMES)}.\n"
            f"Example: mashu task close {' '.join(args.ref)} --outcome completed\n"
            "For the interactive chooser, run mashu task close with no task ID."
        )
    # Commit each task separately so a later invalid reference does not undo earlier closes.
    for ref in args.ref:
        with db.transaction(args.dsn) as cur:
            task_id = _task_ref(cur, ref)
            row = task_actions.close_task(
                cur, task_id, outcome=args.outcome, actor=ACTOR, reason=args.reason
            )
        _gate_warnings(row)
        print(f"closed  {_short(task_id)}  {row['task']['outcome']}")
    return 0


def cmd_task_reopen(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        task_id = _task_ref(cur, args.ref)
        row = tasks.reopen(cur, task_id, actor=ACTOR)
    print(f"reopened  {_short(task_id)}  active to {_date(row['task']['active_until'])}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    for filename in migration.migrate(args.dsn):
        print(f"applied: {filename}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if args.dsn is not None:
        os.environ[db.DSN_ENV_VAR] = args.dsn
    if args.agent:
        os.environ["MASHU_AGENT"] = args.agent
    from mashu import server

    server.main()
    return 0


#: Said wherever an id is taken.
_REF_HELP = "the row's id, or the first four or more characters of it"


_TOP_LEVEL_HELP = """\
Mashu keeps durable rules, evidence of costly forgetting, and current project work.

Start here:
  mashu                     Open the human dashboard.
  mashu status              Summarize the store and pending review work.
  mashu bootstrap           Show what a session in this directory receives.
  mashu memories            List active durable rules.
  mashu task list           List current work.

Recording information:
  remember                  A User writes a rule or short-lived condition directly.
  pain                      Record an incident or repeated lookup and its prevention.
  trace                     Read dated, unreviewed observations (kept for 30 days).

Managing durable rules:
  review                    A User decides pending candidates.
  show                      Inspect one memory, candidate, or ledger row.
  retire / revise / deliver Withdraw, rewrite, or change delivery of a memory.
  guard                     Print the topic rules that apply before an action.
  topic                     Group rules read only when one kind of work begins.

Work state and routing:
  project / task            Read and manage current project work.
  scope / route             Map working directories to knowledge scopes.

Run `mashu COMMAND --help` for arguments and examples. Nested commands use
`mashu project COMMAND --help` or `mashu task COMMAND --help`.

IDs shown by list commands are shortened to eight characters. Any unique prefix
of four or more characters is accepted wherever help says REF or ID.

Environment:
  MASHU_DATABASE_URL         PostgreSQL connection string (default: dbname=mashu).
  MASHU_AGENT                Agent label used by `mashu serve` when --agent is absent.

This is the human-facing CLI. Agents should normally use the Mashu MCP tools;
project creation, direct remembering, review decisions, task closure, and task
reopening remain User decisions even when the command is technically callable.
"""


class _HelpFormatter(argparse.HelpFormatter):
    """Wrap prose while preserving indented command examples and section breaks."""

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        rendered: list[str] = []
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph:
                return
            rendered.append(
                textwrap.fill(
                    " ".join(line.strip() for line in paragraph),
                    width,
                    initial_indent=indent,
                    subsequent_indent=indent,
                )
            )
            paragraph.clear()

        for line in text.splitlines():
            if not line:
                flush_paragraph()
                rendered.append("")
            elif line.startswith("  "):
                flush_paragraph()
                rendered.append(f"{indent}{line}")
            else:
                paragraph.append(line)
        flush_paragraph()
        return "\n".join(rendered)


def _command(
    action: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    summary: str,
    *,
    description: str | None = None,
    examples: tuple[str, ...] = (),
) -> argparse.ArgumentParser:
    """Add one command with useful help at both parser levels."""
    epilog = None
    if examples:
        epilog = "examples:\n" + "\n".join(f"  {example}" for example in examples)
    return action.add_parser(
        name,
        help=summary,
        description=description or summary,
        epilog=epilog,
        formatter_class=_HelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _MashuArgumentParser(
        prog="mashu",
        description="The knowledge state that keeps only what forgetting has cost something.",
        epilog=_TOP_LEVEL_HELP,
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--dsn", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    _command(
        sub,
        "status",
        "stock, seats used, pending count, recent ledger",
        examples=("mashu status",),
    ).set_defaults(func=cmd_status)
    _command(
        sub,
        "bootstrap",
        "what a session in this directory is pushed, and its token cost",
        description=(
            "Show the active memories, task cards, and temporary context delivered to a "
            "session started in the current working directory."
        ),
        examples=("mashu bootstrap",),
    ).set_defaults(func=cmd_bootstrap)

    remember = _command(
        sub,
        "remember",
        "write a rule straight into the active set (the only immediate path)",
        description=(
            "Write a User-confirmed rule directly into active memory. Use --until instead for "
            "a temporary condition; it cannot be combined with --scope, --delivery, "
            "--topic, or --force."
        ),
        examples=(
            'mashu remember "Run migrations before restarting the service"',
            'mashu remember "The staging host is down" --until 2d',
            'mashu remember "Keep the win rate between 40 and 60 percent" --topic difficulty',
        ),
    )
    remember.add_argument("body", help="the rule, written as a short sentence")
    remember.add_argument("--scope", help="deliver it to this scope only")
    remember.add_argument(
        "--delivery",
        choices=memories.DELIVERIES,
        help=(
            "where it is delivered (default: topic with --topic, scope with --scope, "
            "otherwise always)"
        ),
    )
    remember.add_argument(
        "--topic", help="file it under this topic, read when the topic's work begins"
    )
    remember.add_argument(
        "--until", help="record a dated condition expiring in N days instead (at most 14)"
    )
    remember.add_argument(
        "--force",
        action="store_true",
        help="write it back over a retirement whose reason you have already read",
    )
    remember.set_defaults(func=cmd_remember)

    retire = _command(
        sub,
        "retire",
        "withdraw a memory, leaving the reason as its tombstone",
        examples=(
            'mashu retire 1a2b3c4d --kind invalidated --reason "The service no longer exists"',
        ),
    )
    retire.add_argument("memory_id", help=_REF_HELP)
    retire.add_argument(
        "--reason", required=True, help="why it is retired; later matches read this"
    )
    retire.add_argument(
        "--kind",
        choices=("invalidated", "superseded", "out_of_scope", "relocated"),
        required=True,
        help="why it is retired",
    )
    retire.add_argument(
        "--superseded-by", help="successor Memory ID or unique prefix for superseded"
    )
    retire.add_argument(
        "--temporary-context",
        help="destination Temporary Context ID or unique prefix for relocated",
    )
    retire.set_defaults(func=cmd_retire)

    revise = _command(
        sub,
        "revise",
        "rewrite a memory's body, keeping the old one on file",
        description=(
            "Rewrite a memory while retaining its revision history. Without --content, Mashu "
            "opens $VISUAL, then $EDITOR, then vi. This is a User decision."
        ),
        examples=(
            "mashu revise 1a2b3c4d",
            'mashu revise 1a2b3c4d --content "Use the repository toolchain"',
        ),
    )
    revise.add_argument("memory_id", help=_REF_HELP)
    revise.add_argument("--content", help="the new body; without it, an editor opens on the old")
    revise.set_defaults(func=cmd_revise)

    show = _command(
        sub,
        "show",
        "one memory, candidate, or ledger row in full",
        examples=("mashu show 1a2b3c4d",),
    )
    show.add_argument("ref", help=f"{_REF_HELP}, in any of the three tables")
    show.set_defaults(func=cmd_show)

    memories_parser = _command(
        sub,
        "memories",
        "list the memories held",
        examples=(
            "mashu memories",
            "mashu memories --scope deployment",
            "mashu memories --retired",
        ),
    )
    memories_parser.add_argument("--scope", help="only the memories belonging to this scope")
    memories_parser.add_argument(
        "--retired", action="store_true", help="list the withdrawn ones, with their reasons"
    )
    memories_parser.set_defaults(func=cmd_memories)

    pain = _command(
        sub,
        "pain",
        "record a pain in the ledger and see what it resembles",
        description=(
            "Record what costly forgetting caused and the knowledge or one-time work that would "
            "prevent it. Rules may become review candidates after repeated evidence; work is "
            "filed into a task's next actions when --task is supplied."
        ),
        examples=(
            'mashu pain --kind incident --what "Deployed twice" '
            '--prevention "Check the release ledger"',
            'mashu pain --kind friction --what "Looked up the quota again" '
            '--prevention "Quota resets at midnight"',
            'mashu pain --kind incident --what "Bad export" '
            '--prevention "Validate the manifest" --prevention-kind work --task 1a2b3c4d',
        ),
    )
    pain.add_argument(
        "--kind",
        choices=("incident", "friction"),
        required=True,
        help="wrong work done (incident), or the same thing looked up again (friction)",
    )
    pain.add_argument("--what", required=True, help="what went wrong")
    pain.add_argument(
        "--prevention", required=True, help="what would have had to be known; the matching key"
    )
    pain.add_argument("--scope", help="the scope it happened in")
    pain.add_argument(
        "--prevention-kind",
        choices=ledger_domain.PREVENTION_KINDS,
        default="rule",
        help="a sentence to be held every time (rule, the default), or a change made once (work)",
    )
    pain.add_argument(
        "--task",
        help="for --prevention-kind work: the task whose next actions the change joins",
    )
    pain.set_defaults(func=cmd_pain)

    ledger = _command(
        sub,
        "ledger",
        "read the pain ledger, newest first",
        examples=("mashu ledger", "mashu ledger --scope deployment --limit 50"),
    )
    ledger.add_argument("--limit", type=int, default=20, help="how many rows to show (default 20)")
    ledger.add_argument("--scope", help="only the pains recorded in this scope")
    ledger.set_defaults(func=cmd_ledger)

    trace = _command(
        sub,
        "trace",
        "read and search the traces (dated, unreviewed, 30 days)",
        description=(
            "Read recent observations. Traces are dated, unreviewed evidence and expire after "
            "30 days; they are not durable rules."
        ),
        examples=("mashu trace", 'mashu trace "quota reset" --scope deployment'),
    )
    trace.add_argument("query", nargs="?", help="text to match; without it, the recent traces")
    trace.add_argument("--scope", help="only the traces left in this scope")
    trace.set_defaults(func=cmd_trace)

    review = _command(
        sub,
        "review",
        "decide the pending candidates, one at a time",
        description=(
            "Open the interactive review UI for pending Memory nominations or proposed changes, "
            "list either queue, or make direct CLI User decisions by reference."
        ),
        examples=(
            "mashu review",
            "mashu review --list --all",
            "mashu review --admit 1a2b3c4d --version 3 --delivery scope --scope deployment",
            "mashu review --changes",
            "mashu review --changes --list",
            'mashu review --decline 1a2b3c4d --reason "Too specific to one run"',
        ),
    )
    review_group = review.add_mutually_exclusive_group()
    review_group.add_argument("--list", action="store_true", help="print the queue and stop")
    review_group.add_argument(
        "--admit",
        metavar="REF",
        help=f"admit one candidate at the displayed --version: {_REF_HELP}",
    )
    review_group.add_argument("--decline", metavar="REF", help=f"turn one down: {_REF_HELP}")
    review_group.add_argument(
        "--apply-change", metavar="REF", help=f"apply one reviewed Memory change: {_REF_HELP}"
    )
    review_group.add_argument(
        "--decline-change", metavar="REF", help=f"decline one Memory change: {_REF_HELP}"
    )
    review_group.add_argument(
        "--withdraw-change", metavar="REF", help=f"withdraw one Memory change: {_REF_HELP}"
    )
    review.add_argument("--changes", action="store_true", help="open or list Memory changes")
    review.add_argument(
        "--version",
        type=int,
        help="nomination version shown by review --list, or change version shown by --changes",
    )
    review.add_argument(
        "--request-id",
        type=UUID,
        help="reuse this UUID to replay one nomination admission or Memory change apply",
    )
    review.add_argument(
        "--delivery",
        choices=memories.DELIVERIES,
        help="where the admitted memory is delivered",
    )
    review.add_argument("--scope", help="the scope the admitted memory belongs to")
    review.add_argument("--topic", help="the topic it is read with, for topic")
    review.add_argument(
        "--ack-conflict",
        action="append",
        type=UUID,
        help="explicitly acknowledge a displayed invalidated or legacy conflict (repeat per id)",
    )
    review.add_argument("--reason", help="why it is turned down; required with --decline")
    review.add_argument(
        "--all", action="store_true", help="include the candidates that were put off"
    )
    review.set_defaults(func=cmd_review)

    deliver = _command(
        sub,
        "deliver",
        "move a memory between the opening, a scope, and a topic",
        description=(
            "Change where an active memory is delivered. Scope delivery requires --scope; "
            "topic delivery requires --topic."
        ),
        examples=(
            "mashu deliver 1a2b3c4d always",
            "mashu deliver 1a2b3c4d scope --scope deployment",
            "mashu deliver 1a2b3c4d topic --topic difficulty",
        ),
    )
    deliver.add_argument("memory_id", help=_REF_HELP)
    deliver.add_argument(
        "delivery", choices=memories.DELIVERIES, help="where it is delivered from now on"
    )
    deliver.add_argument("--topic", help="the topic it is read with, for topic")
    deliver.add_argument("--scope", help="the scope it belongs to, for scope")
    deliver.add_argument(
        "--no-scope",
        action="store_true",
        help="drop the scope it carried, so an always rule belongs to no scope",
    )
    deliver.set_defaults(func=cmd_deliver)

    guard = _command(
        sub,
        "guard",
        "the topic rules that apply before one action, for the PreToolUse hook",
        description=(
            "Print the active rules of the topic linked to ACTION (mashu topic --edit NAME "
            "--action ACTION) when that topic is listed in this directory's sessions. Rules "
            "exit 2 so a hook can show them and ask the caller to retry; none exits 0."
        ),
        examples=(
            "mashu guard delegate",
            "mashu guard delegate --json",
        ),
    )
    guard.add_argument("action", help="the action a tool call carries out, such as delegate")
    guard.add_argument("--json", action="store_true", help="print as JSON, for the hook to read")
    guard.set_defaults(func=cmd_guard)

    scope = _command(
        sub,
        "scope",
        "the scope register",
        description=(
            "List scopes, or create one with --add and an optional --about. Creation is User-only."
        ),
        examples=(
            "mashu scope",
            "mashu scope --add deployment",
            'mashu scope --add deployment --about "Production releases"',
        ),
    )
    scope.add_argument("--add", metavar="NAME", help="create a scope with this name")
    scope.add_argument("--about", metavar="LINE", help="what the scope covers")
    scope.set_defaults(func=cmd_scope)

    topic = _command(
        sub,
        "topic",
        "rules read only when one kind of work begins",
        description=(
            "List topics, or create, edit, or remove one. A topic's trigger line is pushed at "
            "session start and its rules are read when that work begins. --add requires "
            "--trigger; --scope limits the listing to one Scope, and '-' with --edit lists it "
            "in every session. Removing needs a topic with no active rules: one never used is "
            "deleted, one whose past rules name it is archived. --action with --edit links the "
            "topic to an action such as delegate (an Agent or Task tool call), so the "
            "PreToolUse hook shows its rules before that call; one action leads to one open "
            "topic. These are User operations."
        ),
        examples=(
            "mashu topic",
            'mashu topic --add difficulty --trigger "Before changing difficulty levers" '
            "--scope game",
            'mashu topic --edit difficulty --trigger "Before changing win rates"',
            "mashu topic --edit difficulty --scope -",
            "mashu topic --edit delegation --action delegate",
            "mashu topic --remove difficulty",
            "mashu topic show difficulty",
        ),
    )
    topic_group = topic.add_mutually_exclusive_group()
    topic_group.add_argument("--add", metavar="NAME", help="create a topic with this name")
    topic_group.add_argument("--edit", metavar="NAME", help="edit the topic with this name")
    topic_group.add_argument(
        "--remove", metavar="NAME", help="delete an unused topic, or archive one used before"
    )
    topic.add_argument("--trigger", metavar="TEXT", help="one sentence saying when to read it")
    topic.add_argument(
        "--scope", help="the Scope whose sessions list it; '-' with --edit means every session"
    )
    topic.add_argument("--rename", metavar="NEW", help="the new name, with --edit")
    topic_action = topic.add_mutually_exclusive_group()
    topic_action.add_argument(
        "--action", metavar="ACT", help="with --edit, the action whose tool calls it precedes"
    )
    topic_action.add_argument(
        "--no-action", action="store_true", help="with --edit, unlink it from its action"
    )
    topic.set_defaults(func=cmd_topic, topic_command=None)
    topic_sub = topic.add_subparsers(dest="topic_command", metavar="COMMAND")
    topic_show = _command(
        topic_sub,
        "show",
        "one topic and the bodies of its rules",
        description="Print one topic's trigger and rules for a person. Nothing is recorded.",
        examples=("mashu topic show difficulty",),
    )
    topic_show.add_argument("name", help="the topic's name")
    topic_show.set_defaults(func=cmd_topic)

    route = _command(
        sub,
        "route",
        "which working directory means which scope",
        description=(
            "List directory-prefix routes, add or remove one, or explicitly mark a prefix as "
            "unscoped. --add requires --scope."
        ),
        examples=(
            "mashu route",
            "mashu route --add /work/service --scope deployment",
            "mashu route --ignore /work/scratch",
            "mashu route --remove /work/service",
        ),
    )
    route_group = route.add_mutually_exclusive_group()
    route_group.add_argument("--add", metavar="PATH", help="route this path prefix to --scope")
    route_group.add_argument(
        "--ignore", metavar="PATH", help="record that this path prefix has no scope"
    )
    route_group.add_argument("--remove", metavar="PATH", help="drop the route for this path prefix")
    route.add_argument("--scope", help="the scope to route to; required with --add")
    route.set_defaults(func=cmd_route)

    project = _command(
        sub,
        "project",
        "the projects work state is filed under",
        description=(
            "List, create, or inspect projects. Projects group tasks; scopes control knowledge "
            "delivery, so the two are related but not interchangeable."
        ),
        examples=(
            "mashu project list",
            "mashu project create website --scope frontend",
            "mashu project show website",
        ),
    )
    project_sub = project.add_subparsers(dest="project_command", required=True, metavar="COMMAND")
    _command(
        project_sub,
        "list",
        "every project, and how many tasks it is carrying",
        examples=("mashu project list",),
    ).set_defaults(func=cmd_project_list)
    project_create = _command(
        project_sub,
        "create",
        "open a project (User only)",
        examples=("mashu project create website --scope frontend",),
    )
    project_create.add_argument("name", help="what its tasks are filed under")
    project_create.add_argument("--scope", help="the scope this project's sessions run in")
    project_create.set_defaults(func=cmd_project_create)
    project_show = _command(
        project_sub,
        "show",
        "one project and the tasks still open in it",
        examples=("mashu project show website", "mashu project show 1a2b3c4d"),
    )
    project_show.add_argument("ref", help=f"the project's name, or {_REF_HELP}")
    project_show.set_defaults(func=cmd_project_show)

    task = _command(
        sub,
        "task",
        "the work that is current, and how current it is",
        description=(
            "List, inspect, create, refresh, close, or reopen tasks. Agents update task state "
            "through MCP; closing and reopening are User decisions."
        ),
        examples=(
            "mashu task list",
            "mashu task show 1a2b3c4d",
            'mashu task create "Add CLI help" --project mashu '
            '--goal "Every command explains itself"',
        ),
    )
    task_sub = task.add_subparsers(dest="task_command", required=True, metavar="COMMAND")

    task_listing = _command(
        task_sub,
        "list",
        "the tasks, each under its own date",
        description=(
            "List active tasks by default. --dormant shows open tasks whose lease expired; "
            "--closed shows ended tasks."
        ),
        examples=("mashu task list", "mashu task list --dormant --project mashu"),
    )
    task_which = task_listing.add_mutually_exclusive_group()
    task_which.add_argument(
        "--dormant", action="store_true", help="the open tasks whose lease has run out"
    )
    task_which.add_argument(
        "--closed", action="store_true", help="the tasks somebody has already ended"
    )
    task_listing.add_argument("--project", help="only the tasks filed under this project")
    task_listing.set_defaults(func=cmd_task_list)

    task_show = _command(
        task_sub,
        "show",
        "one task in full, with its history and artifacts",
        examples=("mashu task show 1a2b3c4d",),
    )
    task_show.add_argument("ref", help=_REF_HELP)
    task_show.set_defaults(func=cmd_task_show)

    task_create = _command(
        task_sub,
        "create",
        "open a task, matched against the open ones",
        description=(
            "Create a task after checking for similar open work. The project may be named "
            "explicitly or inferred from the current directory's route."
        ),
        examples=(
            'mashu task create "Add CLI help" --project mashu '
            '--goal "Every command explains itself"',
        ),
    )
    task_create.add_argument("name", help="the work, named as the duplicate match will read it")
    task_create.add_argument(
        "--project", help="where it is filed; without it, the project this directory routes to"
    )
    task_create.add_argument("--goal", help="what finishing it would mean")
    task_create.add_argument(
        "--force", action="store_true", help="open it even though one already reads like it"
    )
    task_create.set_defaults(func=cmd_task_create)

    task_touch = _command(
        task_sub,
        "touch",
        "say the work is still current, nothing more",
        examples=("mashu task touch 1a2b3c4d",),
    )
    task_touch.add_argument("ref", help=_REF_HELP)
    task_touch.set_defaults(func=cmd_task_touch)

    task_close = _command(
        task_sub,
        "close",
        "end tasks (User only), on a chosen outcome",
        description=(
            "Close tasks with an explicit outcome. Silence or an expired lease is not closure; "
            "this command records the User's decision. With no task named it opens the closing "
            "screen: every open task on one list, the outcome chosen per task in a keystroke, "
            "and a close proposal left by an agent taken with a single key."
        ),
        examples=(
            "mashu task close",
            'mashu task close 1a2b3c4d --outcome completed --reason "Released in v2.1"',
            "mashu task close 1a2b3c4d 5e6f7a8b --outcome superseded",
        ),
    )
    task_close.add_argument("ref", nargs="*", help=f"the tasks to end; {_REF_HELP}")
    task_close.add_argument(
        "--outcome", choices=tasks.OUTCOMES, help="whether it was finished, given up, or replaced"
    )
    task_close.add_argument("--reason", help="what a later reader would want to know about the end")
    task_close.add_argument(
        "--project", help="for the closing screen: only the tasks filed under this project"
    )
    task_close.set_defaults(func=cmd_task_close)

    task_reopen = _command(
        task_sub,
        "reopen",
        "take back a closure (User only)",
        examples=("mashu task reopen 1a2b3c4d",),
    )
    task_reopen.add_argument("ref", help=_REF_HELP)
    task_reopen.set_defaults(func=cmd_task_reopen)

    serve = _command(
        sub,
        "serve",
        "run the MCP server on stdio",
        description=(
            "Run Mashu as an MCP stdio server. MCP client configuration normally launches this "
            "command; --agent labels agent-originated writes and overrides MASHU_AGENT."
        ),
        examples=("mashu serve --agent codex",),
    )
    serve.add_argument("--agent", help="the name writes are attributed to")
    serve.set_defaults(func=cmd_serve)

    admin = _command(
        sub,
        "admin",
        "store maintenance",
        description="Administrative operations; inspect a nested command's help before running it.",
        examples=("mashu admin migrate",),
    )
    admin_sub = admin.add_subparsers(dest="admin_command", required=True, metavar="COMMAND")
    _command(
        admin_sub,
        "migrate",
        "apply the migrations not yet applied",
        examples=("mashu admin migrate",),
    ).set_defaults(func=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser._mashu_arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(parser._mashu_arguments)
    try:
        if args.command is None:
            from mashu import home_ui

            return home_ui.run(args.dsn)
        return args.func(args)
    except MashuError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as error:
        # A pending migration adds tables and columns alike; explain either instead of raising.
        print(_behind(args) or str(error), file=sys.stderr)
        return 1


def _behind(args: argparse.Namespace) -> str | None:
    """The sentence `status` prints, for a command that has just failed on it."""
    try:
        with db.transaction(args.dsn) as cur:
            unapplied = migration.pending(cur)
    except Exception:  # noqa: BLE001 - the database is what just failed
        return None
    if not unapplied:
        return None
    return (
        f"this store is behind the code: {len(unapplied)} migration(s) pending "
        f"({', '.join(unapplied)}). Run 'mashu admin migrate'"
    )


if __name__ == "__main__":
    sys.exit(main())
