"""The desktop app's way into the store: one JSON request per line on stdin.

The app starts this as its own child process and talks to it over the pipes, so there is
no port another program could reach. Each request runs in a transaction of its own,
through the same functions the terminal screens call, and is answered on one line:

    {"id": 7, "op": "review.admit", "args": {...}}
    {"id": 7, "ok": true, "result": {...}}
    {"id": 7, "ok": false, "error": "...", ...}
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, TextIO
from uuid import UUID

import psycopg

from mashu import application, db, nominations, review_actions, task_actions, tasks, topics
from mashu.errors import MashuError
from mashu.server import _failure, _plain

ACTOR = "user"


def _uuid(args: dict[str, Any], name: str) -> UUID:
    try:
        return UUID(str(args[name]))
    except (KeyError, ValueError) as error:
        raise MashuError(f"{name} must be a UUID") from error


def _text(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MashuError(f"{name} must be non-empty text")
    return value.strip()


def status(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "pending_ready": snapshot.pending_ready,
        "pending_deferred": snapshot.pending_deferred,
        "close_proposals": proposals,
        "schema_pending": list(snapshot.schema_pending),
    }


def review_list(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    include_deferred = bool(args.get("include_deferred"))
    rows = nominations.pending_nominations(cur, include_deferred=include_deferred)
    for row in rows:
        row["blocking_conflict_ids"] = review_actions.blocking_conflict_ids(row)
    return {
        "candidates": rows,
        "hidden_deferred": 0 if include_deferred else nominations.deferred_count(cur),
    }


def review_admit(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    delivery = args.get("delivery")
    if delivery not in ("always", "scope", "topic"):
        raise MashuError("delivery must be always, scope, or topic")
    topic = {"topic_id": _uuid(args, "topic_id"), "name": None} if delivery == "topic" else None
    version = args.get("expected_version")
    if not isinstance(version, int):
        raise MashuError("expected_version must be an integer")
    outcome = review_actions.admit(
        cur,
        _uuid(args, "nomination_id"),
        expected_version=version,
        delivery=delivery,
        topic=topic,
        conflict_ids=[UUID(str(item)) for item in args.get("conflict_ids") or []],
        actor=ACTOR,
    )
    if isinstance(outcome, review_actions.Changed):
        return {"changed": outcome.what, "current": outcome.current}
    return {"admitted": outcome}


def review_revise(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    return nominations.revise(
        cur, _uuid(args, "nomination_id"), content=_text(args, "content"), actor=ACTOR
    )


def review_decline(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    return nominations.decline(
        cur, _uuid(args, "nomination_id"), actor=ACTOR, reason=_text(args, "reason")
    )


def review_defer(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    return nominations.defer(
        cur, _uuid(args, "nomination_id"), actor=ACTOR, reason=_text(args, "reason")
    )


def _when(value: Any, name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as error:
        raise MashuError(f"{name} must be an ISO timestamp") from error


def task_list(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    """Open tasks, standing proposals first, then overtaken ones, then the rest."""

    def rank(row: dict[str, Any]) -> int:
        proposal = row["proposal"]
        return 2 if proposal is None else int(proposal["stale"])

    rows = sorted(tasks.task_list(cur, activity="open"), key=rank)
    return {"tasks": rows, "outcomes": list(tasks.OUTCOMES)}


def task_accept(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
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


def task_close(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    reason = args.get("reason")
    return task_actions.close_task(
        cur,
        _uuid(args, "task_id"),
        outcome=args.get("outcome"),
        reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
        actor=ACTOR,
    )


def task_keep(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    return task_actions.keep_open(cur, _uuid(args, "task_id"), actor=ACTOR)


def task_renew(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    return tasks.touch(cur, _uuid(args, "task_id"), actor=ACTOR)


def topic_list(cur: psycopg.Cursor, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "topics": [
            {"topic_id": row["topic_id"], "name": row["name"], "trigger": row["trigger"]}
            for row in topics.list_topics(cur)
        ]
    }


OPERATIONS: dict[str, Callable[[psycopg.Cursor, dict[str, Any]], dict[str, Any]]] = {
    "status": status,
    "review.list": review_list,
    "review.admit": review_admit,
    "review.revise": review_revise,
    "review.decline": review_decline,
    "review.defer": review_defer,
    "task.list": task_list,
    "task.accept": task_accept,
    "task.close": task_close,
    "task.keep": task_keep,
    "task.renew": task_renew,
    "topic.list": topic_list,
}

#: The operations that only read, run in a READ ONLY transaction.
READS = frozenset({"status", "review.list", "task.list", "topic.list"})


def answer(dsn: str | None, line: str) -> dict[str, Any]:
    """The reply to one request line."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError as error:
        return {"id": None, "ok": False, "error": f"not JSON: {error}"}
    if not isinstance(request, dict):
        return {"id": None, "ok": False, "error": "a request must be a JSON object"}
    request_id = request.get("id")
    operation = OPERATIONS.get(request.get("op"))
    if operation is None:
        return {"id": request_id, "ok": False, "error": f"unknown op {request.get('op')!r}"}
    args = request.get("args") or {}
    if not isinstance(args, dict):
        return {"id": request_id, "ok": False, "error": "args must be a JSON object"}
    opened = db.read_only if request["op"] in READS else db.transaction
    try:
        with opened(dsn) as cur:
            result = operation(cur, args)
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
