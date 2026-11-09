"""The desktop app's way into the store: one JSON request per line on stdin.

The app starts this as its own child process and talks to it over the pipes, so there is
no port another program could reach. Each request runs in a transaction of its own,
through the same functions the terminal screens call, and is answered on one line:

    {"id": 7, "op": "review.admit", "args": {...}}
    {"id": 7, "ok": true, "result": {...}}
    {"id": 7, "ok": false, "error": "...", ...}
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, TextIO
from uuid import UUID

import psycopg

from mashu import (
    application,
    config,
    db,
    memories,
    memory_actions,
    memory_changes,
    migrate,
    nominations,
    projects,
    review_actions,
    routing,
    scopes,
    settings,
    task_actions,
    tasks,
    temporary,
    topics,
)
from mashu.errors import ChangedSinceReadError, MashuError
from mashu.server import _failure, _plain

ACTOR = "user"

Args = dict[str, Any]


# reading the arguments


def _uuid(args: Args, name: str) -> UUID:
    try:
        return UUID(str(args[name]))
    except (KeyError, ValueError) as error:
        raise MashuError(f"{name} must be a UUID") from error


def _uuid_or_none(args: Args, name: str) -> UUID | None:
    return _uuid(args, name) if args.get(name) else None


def _uuids(args: Args, name: str) -> list[UUID] | None:
    values = args.get(name)
    if values is None:
        return None
    try:
        return [UUID(str(value)) for value in values]
    except (TypeError, ValueError) as error:
        raise MashuError(f"{name} must be a list of UUIDs") from error


def _text(args: Args, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MashuError(f"{name} must be non-empty text")
    return value.strip()


def _optional(args: Args, name: str) -> str | None:
    """Text, with empty or missing read as none."""
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MashuError(f"{name} must be text")
    return value.strip() or None


def _lines(args: Args, name: str) -> list[str]:
    values = args.get(name) or []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise MashuError(f"{name} must be a list of text")
    return [value.strip() for value in values if value.strip()]


def _int(args: Args, name: str) -> int:
    value = args.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MashuError(f"{name} must be a whole number")
    return value


def _days(args: Args) -> float:
    try:
        return float(args.get("days"))
    except (TypeError, ValueError) as error:
        raise MashuError(
            f"days must be a number greater than 0 and at most {config.TEMPORARY_MAX_DAYS}"
        ) from error


def _when(value: Any, name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as error:
        raise MashuError(f"{name} must be an ISO timestamp") from error


def _topic(args: Args) -> dict[str, Any] | None:
    """An existing topic by id, a new one described by new_topic, or none."""
    if args.get("topic_id"):
        return {"topic_id": _uuid(args, "topic_id"), "name": None}
    described = args.get("new_topic")
    if isinstance(described, dict):
        return {
            "topic_id": None,
            "name": _text(described, "name"),
            "trigger": _text(described, "trigger"),
            "scope_name": _optional(described, "scope"),
        }
    return None


# home and tray


def status(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """The counts the tray shows and the notifications compare between polls."""
    snapshot = application.status_snapshot(cur)
    proposals = [
        {
            "task_id": row["task"]["task_id"],
            "name": row["task"]["name"],
            "project": row["task"]["project_name"],
            "outcome": row["proposal"]["outcome"],
            "proposed_at": row["proposal"]["proposed_at"],
        }
        for row in tasks.task_list(cur, activity="open")
        if row["proposal"] is not None
    ]
    cur.execute("SELECT count(*) AS n FROM memory_change WHERE status = 'pending'")
    return {
        "pending_ready": snapshot.pending_ready,
        "pending_deferred": snapshot.pending_deferred,
        "memory_changes": cur.fetchone()["n"],
        "close_proposals": proposals,
        "schema_pending": list(snapshot.schema_pending),
    }


def home(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return application.dashboard_counts(cur)


def choices(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """The names and fixed choices the forms offer."""
    return {
        "scopes": [row["name"] for row in scopes.list_scopes(cur)],
        "projects": [row["name"] for row in projects.list_projects(cur)],
        "topics": [
            {"topic_id": row["topic_id"], "name": row["name"], "trigger": row["trigger"]}
            for row in topics.list_topics(cur)
        ],
        "outcomes": list(tasks.OUTCOMES),
        "temporary_max_days": config.TEMPORARY_MAX_DAYS,
    }


# review


def review_list(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    include_deferred = bool(args.get("include_deferred"))
    rows = nominations.pending_nominations(cur, include_deferred=include_deferred)
    for row in rows:
        row["blocking_conflict_ids"] = review_actions.blocking_conflict_ids(row)
    return {
        "candidates": rows,
        "hidden_deferred": 0 if include_deferred else nominations.deferred_count(cur),
    }


def review_admit(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    delivery = args.get("delivery")
    if delivery not in ("always", "scope", "topic"):
        raise MashuError("delivery must be always, scope, or topic")
    topic = _topic(args) if delivery == "topic" else None
    if delivery == "topic" and topic is None:
        raise MashuError("topic delivery needs topic_id or new_topic")
    outcome = review_actions.admit(
        cur,
        _uuid(args, "nomination_id"),
        expected_version=_int(args, "expected_version"),
        delivery=delivery,
        topic=topic,
        conflict_ids=_uuids(args, "conflict_ids") or [],
        actor=ACTOR,
    )
    if isinstance(outcome, review_actions.Changed):
        return {"changed": outcome.what, "current": outcome.current}
    return {"admitted": outcome}


def review_revise(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return nominations.revise(
        cur, _uuid(args, "nomination_id"), content=_text(args, "content"), actor=ACTOR
    )


def review_decline(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return nominations.decline(
        cur, _uuid(args, "nomination_id"), actor=ACTOR, reason=_text(args, "reason")
    )


def review_defer(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return nominations.defer(
        cur, _uuid(args, "nomination_id"), actor=ACTOR, reason=_text(args, "reason")
    )


# Memory changes


def change_list(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    rows = memory_changes.pending(cur)
    for row in rows:
        row["blocking_conflict_ids"] = memory_actions.blocking_conflict_ids(row)
    return {"changes": rows}


def change_apply(dsn: str | None, args: Args) -> dict[str, Any]:
    """Apply the version shown, or re-propose it against what is current and say so."""
    change_id = _uuid(args, "change_id")
    try:
        with db.transaction(dsn) as cur:
            return {
                "applied": memory_actions.apply_change(
                    cur,
                    change_id,
                    version=_int(args, "version"),
                    conflict_ids=_uuids(args, "conflict_ids") or [],
                    actor=ACTOR,
                )
            }
    except ChangedSinceReadError as refusal:
        try:
            with db.transaction(dsn) as cur:
                refreshed = memory_actions.refresh_change(cur, change_id, actor=ACTOR)
        except MashuError:
            raise refusal from None
        return {"changed": True, "current": refreshed}


def change_reason(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.edit_change_reason(
        cur,
        _uuid(args, "change_id"),
        version=_int(args, "version"),
        target_revision_id=_uuid_or_none(args, "target_revision_id"),
        reason=_text(args, "reason"),
        actor=ACTOR,
    )


def _change_decision(status: str) -> Callable[[psycopg.Cursor, Args], dict[str, Any]]:
    def decide(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
        return memory_changes.decide(
            cur, _uuid(args, "change_id"), status=status, actor=ACTOR, reason=_text(args, "reason")
        )

    return decide


# Memories and Temporary Context


def memory_list(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    view = args.get("view", "active")
    if view not in ("active", "retired", "temporary"):
        raise MashuError("view must be active, retired, or temporary")
    return {"rows": application.memory_view(cur, view)}


def memory_get(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    row = memories.memory_details(cur, _uuid(args, "memory_id"))
    if row is None:
        raise MashuError(f"no memory {args.get('memory_id')}")
    return row


def memory_remember(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.remember(
        cur,
        content=_text(args, "content"),
        why=_optional(args, "why"),
        delivery=_text(args, "delivery"),
        scope_name=_optional(args, "scope"),
        topic=_topic(args),
        acknowledged=_uuids(args, "acknowledged"),
        actor=ACTOR,
    )


def memory_revise(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memories.revise(
        cur,
        _uuid(args, "memory_id"),
        content=_text(args, "content"),
        actor=ACTOR,
        acknowledged_conflicts=_uuids(args, "acknowledged"),
    )


def memory_add_reason(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memories.add_reason(cur, _uuid(args, "memory_id"), why=_text(args, "why"), actor=ACTOR)


def memory_restore(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.restore(
        cur,
        _uuid(args, "memory_id"),
        reason=_text(args, "reason"),
        acknowledged=_uuids(args, "acknowledged"),
        actor=ACTOR,
    )


def memory_retire(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.retire(
        cur, _uuid(args, "memory_id"), reason=_text(args, "reason"), actor=ACTOR
    )


def memory_delivery(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.set_delivery(
        cur,
        _uuid(args, "memory_id"),
        delivery=_text(args, "delivery"),
        scope_name=_optional(args, "scope"),
        topic=_topic(args),
        actor=ACTOR,
    )


def memory_to_temporary(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memories.convert_to_temporary(
        cur, _uuid(args, "memory_id"), days=_days(args), actor=ACTOR
    )


def temporary_add(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.add_temporary(
        cur, content=_text(args, "content"), days=_days(args), actor=ACTOR
    )


def temporary_revise(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return memory_actions.revise_temporary(
        cur,
        _uuid(args, "context_id"),
        content=_text(args, "content"),
        scope_name=_optional(args, "scope"),
        actor=ACTOR,
    )


def temporary_end(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return temporary.end(cur, _uuid(args, "context_id"), actor=ACTOR)


def temporary_to_memory(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return temporary.convert_to_memory(
        cur,
        _uuid(args, "context_id"),
        actor=ACTOR,
        acknowledged_conflicts=_uuids(args, "acknowledged"),
    )


# Work: tasks and projects


def work_tasks(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """One task view with history; open views lead with standing, then overtaken, proposals."""
    activity = args.get("activity", "active")
    if activity not in ("active", "dormant", "closed"):
        raise MashuError("activity must be active, dormant, or closed")

    def rank(row: dict[str, Any]) -> int:
        proposal = row["proposal"]
        return 2 if proposal is None else int(proposal["stale"])

    rows = sorted(task_actions.task_view(cur, activity), key=rank)
    for row in rows:
        row["card_capacity"] = config.task_card_capacity()
        row["detail_capacity"] = config.task_detail_capacity()
    return {"tasks": rows}


def work_projects(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return {"projects": projects.list_projects(cur)}


def task_create(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return task_actions.create_task(
        cur,
        name=_text(args, "name"),
        project=_optional(args, "project"),
        goal=_optional(args, "goal"),
        cwd=None,
        actor=ACTOR,
        force=bool(args.get("force")),
    )


def task_update(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """Set every editable field to what the form holds; empty clears a field."""
    return tasks.task_update(
        cur,
        _uuid(args, "task_id"),
        actor=ACTOR,
        expect_updated_at=_when(args.get("expect_updated_at"), "expect_updated_at"),
        name=_text(args, "name"),
        project=_text(args, "project"),
        goal=args.get("goal") or "",
        approach=args.get("approach") or "",
        status_text=args.get("status_text") or "",
        open_questions=_lines(args, "open_questions"),
        blockers=_lines(args, "blockers"),
        next_actions=_lines(args, "next_actions"),
    )


def task_accept(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """Close on exactly the proposal, and against exactly the state, that were shown."""
    shown = args.get("proposal")
    if not isinstance(shown, dict):
        raise MashuError("proposal must be the proposal as it was shown")
    expected = {
        **shown,
        "state_at": _when(shown.get("state_at"), "proposal.state_at"),
        "proposed_at": _when(shown.get("proposed_at"), "proposal.proposed_at"),
    }
    return task_actions.accept_close_proposal(
        cur,
        _uuid(args, "task_id"),
        expected_proposal=expected,
        expected_state=_when(args.get("state_at"), "state_at"),
        actor=ACTOR,
    )


def task_close(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return task_actions.close_task(
        cur,
        _uuid(args, "task_id"),
        outcome=args.get("outcome"),
        reason=_optional(args, "reason"),
        actor=ACTOR,
    )


def task_keep(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return task_actions.keep_open(cur, _uuid(args, "task_id"), actor=ACTOR)


def task_renew(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return tasks.touch(cur, _uuid(args, "task_id"), actor=ACTOR)


def task_reopen(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return tasks.reopen(cur, _uuid(args, "task_id"), actor=ACTOR)


def project_save(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return task_actions.save_project(
        cur,
        _uuid_or_none(args, "project_id"),
        name=_text(args, "name"),
        scope_name=_optional(args, "scope"),
        actor=ACTOR,
    )


# Settings and health


def health(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    # A rule that did not prevent a pain and a task or file whose knowledge went unused are
    # one kind of failure, and one report can name both, so the pains are counted once.
    cur.execute(
        "SELECT count(DISTINCT ledger_id) AS n FROM event_log "
        "WHERE event_type IN ('delivery_failure_suspected', 'knowledge_unused') "
        "AND created_at >= now() - interval '30 days'"
    )
    missed = cur.fetchone()["n"]
    return {
        **dataclasses.asdict(application.status_snapshot(cur)),
        "missed_30d": missed,
        "capacities": {
            "always": config.always_capacity(),
            "worst": config.capacity(),
            "project": config.project_capacity(),
            "temporary": config.temporary_capacity(),
            "topic": config.topic_capacity(),
        },
        "failure_routes": list(application.DELIVERY_FAILURE_ROUTES),
    }


def bootstrap_preview(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """What a session opened in the directory given receives; it is recorded as served."""
    return application.bootstrap_preview(cur, cwd=_text(args, "cwd"), actor=ACTOR)


def scope_list(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return {"scopes": scopes.list_scopes(cur)}


def scope_save(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    scope_id = _uuid_or_none(args, "scope_id")
    name, summary = _text(args, "name"), _optional(args, "summary")
    if scope_id is None:
        return scopes.create_scope(cur, name=name, summary=summary, actor=ACTOR)
    return scopes.update_scope(cur, scope_id, name=name, summary=summary, actor=ACTOR)


def topic_list(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    rows = topics.list_topics(cur)
    for row in rows:
        row["source_text"] = topics.source_text(row.get("source"))
    return {"topics": rows, "capacity": config.topic_capacity()}


def topic_save(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return application.save_topic(
        cur,
        _uuid_or_none(args, "topic_id"),
        name=_text(args, "name"),
        trigger=_text(args, "trigger"),
        scope_name=_optional(args, "scope"),
        action=_optional(args, "action"),
        source=_optional(args, "source"),
        actor=ACTOR,
    )


def topic_remove(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return topics.remove_topic(cur, _uuid(args, "topic_id"), actor=ACTOR)


def route_list(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return {"routes": routing.all_routes(cur)}


def route_save(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    """Add or edit a route; no scope means the directory is ignored on purpose."""
    route_id, path = _uuid_or_none(args, "route_id"), _text(args, "path")
    scope_name = _optional(args, "scope")
    if route_id is not None:
        return application.update_route(
            cur, route_id, path_prefix=path, scope_name=scope_name, actor=ACTOR
        )
    if scope_name is None:
        return application.ignore_route(cur, path_prefix=path, actor=ACTOR)
    return application.set_route(cur, path_prefix=path, scope_name=scope_name, actor=ACTOR)


def route_remove(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    path = _text(args, "path")
    return {"removed": application.remove_route(cur, path_prefix=path, actor=ACTOR)}


def interval_get(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return {
        "minutes": settings.checkpoint_interval_minutes(cur),
        "max": settings.MAX_CHECKPOINT_INTERVAL_MINUTES,
    }


def interval_set(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    minutes = _int(args, "minutes")
    return {"minutes": settings.set_checkpoint_interval_minutes(cur, minutes, actor=ACTOR)}


def schema_status(cur: psycopg.Cursor, args: Args) -> dict[str, Any]:
    return {"pending": migrate.pending(cur)}


def schema_migrate(dsn: str | None, args: Args) -> dict[str, Any]:
    """Apply every pending migration, each in a transaction of its own."""
    return {"applied": migrate.migrate(dsn)}


Operation = Callable[[psycopg.Cursor, Args], dict[str, Any]]

OPERATIONS: dict[str, Operation] = {
    "status": status,
    "home": home,
    "choices": choices,
    "review.list": review_list,
    "review.admit": review_admit,
    "review.revise": review_revise,
    "review.decline": review_decline,
    "review.defer": review_defer,
    "change.list": change_list,
    "change.reason": change_reason,
    "change.decline": _change_decision("declined"),
    "change.withdraw": _change_decision("withdrawn"),
    "memory.list": memory_list,
    "memory.get": memory_get,
    "memory.remember": memory_remember,
    "memory.revise": memory_revise,
    "memory.add_reason": memory_add_reason,
    "memory.restore": memory_restore,
    "memory.retire": memory_retire,
    "memory.delivery": memory_delivery,
    "memory.to_temporary": memory_to_temporary,
    "temporary.add": temporary_add,
    "temporary.revise": temporary_revise,
    "temporary.end": temporary_end,
    "temporary.to_memory": temporary_to_memory,
    "work.tasks": work_tasks,
    "work.projects": work_projects,
    "task.create": task_create,
    "task.update": task_update,
    "task.accept": task_accept,
    "task.close": task_close,
    "task.keep": task_keep,
    "task.renew": task_renew,
    "task.reopen": task_reopen,
    "project.save": project_save,
    "settings.health": health,
    "settings.bootstrap": bootstrap_preview,
    "scope.list": scope_list,
    "scope.save": scope_save,
    "topic.list": topic_list,
    "topic.save": topic_save,
    "topic.remove": topic_remove,
    "route.list": route_list,
    "route.save": route_save,
    "route.remove": route_remove,
    "interval.get": interval_get,
    "interval.set": interval_set,
    "schema.status": schema_status,
}

#: Operations that open their own transactions, given the connection string instead.
OWN_TRANSACTIONS: dict[str, Callable[[str | None, Args], dict[str, Any]]] = {
    "change.apply": change_apply,
    "schema.migrate": schema_migrate,
}

#: The operations that only read, run in a READ ONLY transaction.
READS = frozenset(
    {
        "status",
        "home",
        "choices",
        "review.list",
        "change.list",
        "memory.list",
        "memory.get",
        "work.tasks",
        "work.projects",
        "settings.health",
        "scope.list",
        "topic.list",
        "route.list",
        "interval.get",
        "schema.status",
    }
)


def answer(dsn: str | None, line: str) -> dict[str, Any]:
    """The reply to one request line."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError as error:
        return {"id": None, "ok": False, "error": f"not JSON: {error}"}
    if not isinstance(request, dict):
        return {"id": None, "ok": False, "error": "a request must be a JSON object"}
    request_id, op = request.get("id"), request.get("op")
    if op not in OPERATIONS and op not in OWN_TRANSACTIONS:
        return {"id": request_id, "ok": False, "error": f"unknown op {op!r}"}
    args = request.get("args") or {}
    if not isinstance(args, dict):
        return {"id": request_id, "ok": False, "error": "args must be a JSON object"}
    try:
        if op in OWN_TRANSACTIONS:
            result = OWN_TRANSACTIONS[op](dsn, args)
        else:
            opened = db.read_only if op in READS else db.transaction
            with opened(dsn) as cur:
                result = OPERATIONS[op](cur, args)
    except MashuError as error:
        return {"id": request_id, **_failure(error)}
    except psycopg.Error as error:
        return {"id": request_id, "ok": False, "error": f"database: {error}".strip()}
    return {"id": request_id, "ok": True, "result": _plain(result)}


def serve(dsn: str | None, source: TextIO | None = None, sink: TextIO | None = None) -> int:
    """Answer requests until the app closes the pipe."""
    if source is None:
        sys.stdin.reconfigure(encoding="utf-8")
        source = sys.stdin
    if sink is None:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sink = sys.stdout
    for line in source:
        if not line.strip():
            continue
        reply = json.dumps(answer(dsn, line), ensure_ascii=False, default=str)
        sink.write(reply + "\n")
        sink.flush()
    return 0
