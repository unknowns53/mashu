"""The human command line for Mashu v2."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from mashu import (
    bootstrap,
    capacity,
    config,
    db,
    events,
    memories,
    nominations,
    routing,
    scopes,
    temporary,
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
from mashu.errors import MashuError, RefusedError

ACTOR = "user"
GUARD_HOLD = 2
_DAYS = re.compile(r"^(\d+(?:\.\d+)?)(?:d)?$")


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


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise MashuError(f"invalid UUID: {value}") from error


def _scope(cur: Any, name: str | None) -> UUID | None:
    return scopes.require_scope(cur, name)["scope_id"] if name is not None else None


def _routed_scope(cur: Any) -> tuple[UUID | None, str | None, bool]:
    scope_id, routed = routing.resolve(cur, os.getcwd())
    if scope_id is None:
        return None, None, routed
    cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
    row = cur.fetchone()
    return scope_id, row["name"] if row else None, routed


def _short(value: Any) -> str:
    return str(value)[:8]


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
            raise MashuError("editor could not be run") from error
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


def _parse_days(value: str) -> float:
    match = _DAYS.fullmatch(value.strip())
    if match is None:
        raise MashuError("--until expects N, Nd, or N.5d")
    days = float(match.group(1))
    if not 0 < days <= 14:
        raise RefusedError("temporary context must be more than 0 and no more than 14 days")
    return days


def _print_memory_rows(rows: list[dict[str, Any]], *, heading: str = "memories") -> None:
    print(heading)
    for row in rows:
        print(f"{_short(row['memory_id']):8}  {row['content']}")


def cmd_status(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        cur.execute(
            "SELECT delivery, count(*) AS count FROM memory "
            "WHERE status = 'active' GROUP BY delivery ORDER BY delivery"
        )
        counts = {row["delivery"]: row["count"] for row in cur.fetchall()}
        totals = capacity.bootstrap_totals(cur)
        cur.execute("SELECT count(*) AS count FROM trace WHERE expires_at > now()")
        trace_count = cur.fetchone()["count"]
        cur.execute(
            "SELECT count(*) AS count FROM ledger WHERE created_at >= now() - interval '30 days'"
        )
        ledger_count = cur.fetchone()["count"]
        pending = nominations.pending_nominations(cur)
        scope_rows = scopes.list_scopes(cur)

    active = "  ".join(f"{key}={counts.get(key, 0)}" for key in ("always", "scope", "guard"))
    print(f"active  {active}")
    print(f"tokens  worst={totals['worst']}  capacity={config.capacity()}")
    print(f"pending {len(pending)}")
    print(f"traces  unexpired={trace_count}")
    print(f"ledger  last_30_days={ledger_count}")
    print("scopes")
    print("name                         active  push_tokens")
    for row in scope_rows:
        print(f"{row['name'][:28]:28}  {row['n_active']:6}  {row['push_tokens']:11}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        scope_id, scope_name, routed = _routed_scope(cur)
        answer = bootstrap.session_bootstrap(
            cur,
            actor=ACTOR,
            scope_id=scope_id,
            scope_name=scope_name,
            routed=routed,
        )
    routed_text = str(answer.get("routed", routed)).lower()
    print(f"scope     {answer.get('scope') or '-'}  routed={routed_text}")
    _print_memory_rows(answer.get("always", []), heading="always")
    _print_memory_rows(answer.get("scoped", []), heading="scoped")
    print("temporary")
    for row in answer.get("temporary", []):
        print(f"  {row['content']}  (expires {row['expires_at']})")
    print(f"tokens    {answer.get('tokens', 0)}/{answer.get('capacity', config.capacity())}")
    print(f"pending   {answer.get('pending', 0)}")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    if args.until is not None:
        if args.scope is not None or args.delivery is not None or args.action is not None:
            raise MashuError("--until cannot be combined with --scope, --delivery, or --action")
        days = _parse_days(args.until)
        with db.transaction(args.dsn) as cur:
            row = temporary.put_temporary(cur, content=args.body, actor=ACTOR, days=days)
        print(f"temporary  {_short(row['context_id'])}  {row['expires_at']}")
        return 0

    delivery = args.delivery or ("scope" if args.scope else "always")
    with db.transaction(args.dsn) as cur:
        scope_id = _scope(cur, args.scope)
        row = memories.remember(
            cur,
            content=args.body,
            actor=ACTOR,
            scope_id=scope_id,
            delivery=delivery,
            guard_action=args.action,
        )
    print(f"remembered  {row['memory_id']}")
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = memories.retire(cur, _uuid(args.memory_id), reason=args.reason, actor=ACTOR)
    print(f"retired  {row['memory_id']}")
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    memory_id = _uuid(args.memory_id)
    content = args.content
    if content is None:
        with db.transaction(args.dsn) as cur:
            row = memories.get_memory(cur, memory_id)
        if row is None:
            raise MashuError(f"memory '{memory_id}' not found")
        content = _editor_text(row["content"])
    with db.transaction(args.dsn) as cur:
        row = memories.revise(cur, memory_id, content=content, actor=ACTOR)
    print(f"revised  {row['memory_id']}")
    return 0


def cmd_pain(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = ledger_domain.report_pain(
            cur,
            kind=args.kind,
            what=args.what,
            prevention=args.prevention,
            actor=ACTOR,
            scope_id=_scope(cur, args.scope),
        )
    matches = row.get("matches", {})
    print(f"pain  {_short(row['ledger_id'])}")
    for name in ("ledger", "traces", "tombstones"):
        print(f"{name}: {len(matches.get(name, []))}")
    if row.get("tombstone_suppressed"):
        print("nomination  withheld: retired knowledge already covers this")
        for stone in matches.get("tombstones", []):
            print(f"  retired {_short(stone['memory_id'])}: {stone['retire_reason']}")
    elif row.get("nomination_existing"):
        print("nomination  existing")
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
        created = row.get("created_at")
        date_text = created.date().isoformat() if hasattr(created, "date") else str(created)[:10]
        print(f"{_short(row['ledger_id']):8}  {row['kind']:<9}  {date_text}  {row['what']}")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        rows = trace_domain.search_traces(
            cur,
            query=args.query,
            scope_id=_scope(cur, args.scope),
        )
    for row in rows:
        print(f"{_short(row['trace_id']):8}  {row['created_at']}  {row['content']}")
    return 0


def _pending_by_id(cur: Any, value: str) -> dict[str, Any]:
    wanted = _uuid(value)
    for row in nominations.pending_nominations(cur):
        if row["nomination_id"] == wanted:
            return row
    raise MashuError(f"pending nomination '{value}' not found")


def _print_pending(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("nothing waiting for review")
    for row in rows:
        print(f"{row['nomination_id']}  {row['kind']}  {row.get('scope_name') or '-'}")
        print(f"  {row['content']}")
        for evidence in row.get("evidence_rows", []):
            print(
                f"  evidence {evidence['kind']} {evidence.get('created_at', '')}: "
                f"{evidence['what']} / {evidence['prevention']}"
            )


def cmd_review(args: argparse.Namespace) -> int:
    if args.list:
        with db.transaction(args.dsn) as cur:
            rows = nominations.pending_nominations(cur)
        _print_pending(rows)
        return 0
    if args.admit:
        with db.transaction(args.dsn) as cur:
            nomination = _pending_by_id(cur, args.admit)
            scope_id = _scope(cur, args.scope) if args.scope else None
            delivery = args.delivery or (
                "scope" if (args.scope or nomination.get("scope_id")) else "always"
            )
            row = nominations.admit(
                cur,
                nomination["nomination_id"],
                actor=ACTOR,
                delivery=delivery,
                scope_id=scope_id,
                guard_action=args.action,
            )
        print(f"admitted  {row['memory_id']}")
        return 0
    if args.decline:
        if not args.reason:
            raise MashuError("--decline requires --reason")
        with db.transaction(args.dsn) as cur:
            nomination = _pending_by_id(cur, args.decline)
            nominations.decline(cur, nomination["nomination_id"], actor=ACTOR, reason=args.reason)
        print(f"declined  {args.decline}")
        return 0

    from mashu import review_ui

    return review_ui.run(args.dsn)


def cmd_deliver(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = memories.set_delivery(
            cur,
            _uuid(args.memory_id),
            delivery=args.delivery,
            actor=ACTOR,
            guard_action=args.action,
            scope_id=_scope(cur, args.scope) if args.scope else None,
        )
    print(f"delivery  {row['memory_id']}  {row['delivery']}")
    return 0


def cmd_guard(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        memory_ref = args.pin or args.unpin
        if memory_ref:
            memory_id = _uuid(memory_ref)
            current = memories.get_memory(cur, memory_id)
            if current is None:
                raise MashuError(f"memory '{memory_ref}' not found")
            if args.pin:
                row = memories.set_delivery(
                    cur,
                    memory_id,
                    delivery="guard",
                    actor=ACTOR,
                    guard_action=args.action,
                )
                print(f"pinned  {row['memory_id']}  {args.action}")
                return 0
            delivery = "scope" if current.get("scope_id") else "always"
            row = memories.set_delivery(
                cur,
                memory_id,
                delivery=delivery,
                actor=ACTOR,
                scope_id=current.get("scope_id"),
            )
            print(f"unpinned  {row['memory_id']}")
            return 0

        scope_id, _, _ = _routed_scope(cur)
        pinned = memories.guard_pins(cur, action=args.action, scope_id=scope_id)
        if pinned:
            events.record(
                cur,
                "guard_served",
                ACTOR,
                detail={"action": args.action, "count": len(pinned)},
            )

    if not pinned:
        if args.json:
            print("[]")
        return 0
    pinned = [{"memory_id": row["memory_id"], "content": row["content"]} for row in pinned]
    if args.json:
        print(json.dumps(_plain(pinned), ensure_ascii=False))
    else:
        for row in pinned:
            print(row["content"])
    return GUARD_HOLD


def cmd_scope(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        if args.add:
            if args.about is None:
                raise MashuError("--add requires --about")
            row = scopes.create_scope(cur, name=args.add, summary=args.about, actor=ACTOR)
            print(f"created  {row['name']}")
            return 0
        rows = scopes.list_scopes(cur)
    print("name                         active  push_tokens")
    for row in rows:
        print(f"{row['name'][:28]:28}  {row['n_active']:6}  {row['push_tokens']:11}")
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        if args.add:
            if args.scope is None:
                raise MashuError("--add requires --scope")
            row = routing.add_route(
                cur,
                path_prefix=args.add,
                scope_id=_scope(cur, args.scope),
                actor=ACTOR,
            )
            print(f"route  {row['path_prefix']}  {args.scope}")
            return 0
        if args.ignore:
            if args.scope is not None:
                raise MashuError("--ignore cannot be combined with --scope")
            row = routing.add_route(cur, path_prefix=args.ignore, scope_id=None, actor=ACTOR)
            print(f"ignored  {row['path_prefix']}")
            return 0
        if args.remove:
            if args.scope is not None:
                raise MashuError("--remove cannot be combined with --scope")
            removed = routing.remove_route(cur, path_prefix=args.remove)
            print("removed" if removed else "not found")
            return 0
        rows = routing.all_routes(cur)
    print("path_prefix                    scope")
    for row in rows:
        print(f"{row['path_prefix'][:28]:28}  {row.get('scope_name') or '(ignored)'}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    for filename in migration.migrate(args.dsn):
        print(f"applied: {filename}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mashu")
    parser.add_argument("--dsn", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("bootstrap").set_defaults(func=cmd_bootstrap)

    remember = sub.add_parser("remember")
    remember.add_argument("body")
    remember.add_argument("--scope")
    remember.add_argument("--delivery", choices=("always", "scope", "guard"))
    remember.add_argument("--action")
    remember.add_argument("--until")
    remember.set_defaults(func=cmd_remember)

    retire = sub.add_parser("retire")
    retire.add_argument("memory_id")
    retire.add_argument("--reason", required=True)
    retire.set_defaults(func=cmd_retire)

    revise = sub.add_parser("revise")
    revise.add_argument("memory_id")
    revise.add_argument("--content")
    revise.set_defaults(func=cmd_revise)

    pain = sub.add_parser("pain")
    pain.add_argument("--kind", choices=("incident", "friction"), required=True)
    pain.add_argument("--what", required=True)
    pain.add_argument("--prevention", required=True)
    pain.add_argument("--scope")
    pain.set_defaults(func=cmd_pain)

    ledger = sub.add_parser("ledger")
    ledger.add_argument("--limit", type=int, default=20)
    ledger.add_argument("--scope")
    ledger.set_defaults(func=cmd_ledger)

    trace = sub.add_parser("trace")
    trace.add_argument("query", nargs="?")
    trace.add_argument("--scope")
    trace.set_defaults(func=cmd_trace)

    review = sub.add_parser("review")
    review_group = review.add_mutually_exclusive_group()
    review_group.add_argument("--list", action="store_true")
    review_group.add_argument("--admit")
    review_group.add_argument("--decline")
    review.add_argument("--delivery", choices=("always", "scope", "guard"))
    review.add_argument("--scope")
    review.add_argument("--action")
    review.add_argument("--reason")
    review.set_defaults(func=cmd_review)

    deliver = sub.add_parser("deliver")
    deliver.add_argument("memory_id")
    deliver.add_argument("delivery", choices=("always", "scope", "guard"))
    deliver.add_argument("--action")
    deliver.add_argument("--scope")
    deliver.set_defaults(func=cmd_deliver)

    guard = sub.add_parser("guard")
    guard.add_argument("action")
    guard_group = guard.add_mutually_exclusive_group()
    guard_group.add_argument("--pin")
    guard_group.add_argument("--unpin")
    guard.add_argument("--json", action="store_true")
    guard.set_defaults(func=cmd_guard)

    scope = sub.add_parser("scope")
    scope.add_argument("--add")
    scope.add_argument("--about")
    scope.set_defaults(func=cmd_scope)

    route = sub.add_parser("route")
    route_group = route.add_mutually_exclusive_group()
    route_group.add_argument("--add")
    route_group.add_argument("--ignore")
    route_group.add_argument("--remove")
    route.add_argument("--scope")
    route.set_defaults(func=cmd_route)

    admin = sub.add_parser("admin")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    admin_sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MashuError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
