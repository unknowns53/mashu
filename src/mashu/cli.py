"""The human command line for Mashu v2."""

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
import unicodedata
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
    tokens,
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

#: What a reference may be made of when it is not a whole id: the characters a
#: UUID prints as, so that pasting any front portion of a displayed id works.
_HEX_REF = re.compile(r"[0-9a-f][0-9a-f-]*")

#: Short enough to type from a listing, long enough that a collision is news
#: rather than routine. Below this the prefix is refused instead of resolved,
#: because a reference that names half the store is not a reference.
_MIN_PREFIX = 4

#: How many colliding ids an ambiguity refusal will name before it stops. The
#: list is there to be re-typed from, and a screenful of them is not.
_AMBIGUITY_LIMIT = 10

#: The tables `show` reaches into, in the order it reports collisions.
_REFERENCE_TABLES = (
    ("memory", "memory_id"),
    ("nomination", "nomination_id"),
    ("ledger", "ledger_id"),
)


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
    """Every id in one table that this reference could be naming.

    Everything this command line prints is an eight-character prefix, so
    everything it accepts has to be one. A whole id is matched as itself; a
    prefix is matched against the printed form of the id, which is the form
    the person is copying from.
    """
    text = (ref or "").strip().lower()
    try:
        exact: str | None = str(UUID(text))
    except (ValueError, AttributeError):
        exact = None
    if exact is None:
        if not _HEX_REF.fullmatch(text):
            raise MashuError(f"'{ref}' is not an id, nor the front of one")
        if len(text) < _MIN_PREFIX:
            raise MashuError(
                f"'{ref}' is too short to name a row: give at least {_MIN_PREFIX} characters"
            )
    clause = f"{id_col} = %(exact)s::uuid" if exact else f"{id_col}::text LIKE %(prefix)s || '%%'"
    if extra_where:
        clause = f"{clause} AND {extra_where}"
    cur.execute(
        f"SELECT {id_col} AS found FROM {table} WHERE {clause} ORDER BY {id_col} LIMIT %(limit)s",
        {"exact": exact, "prefix": text, "limit": _AMBIGUITY_LIMIT},
    )
    return [row["found"] for row in cur.fetchall()]


def _resolve(
    cur: Any, ref: str, *, table: str, id_col: str, label: str, extra_where: str = ""
) -> UUID:
    """The one row this reference names, or a refusal saying why it is not one.

    A whole id passes straight through without a lookup: the command that
    receives it will say soon enough if there is no such row, and its sentence
    is the better one. Only a prefix has to be resolved here, and an ambiguous
    one is refused with its candidates rather than settled arbitrarily.
    """
    try:
        return UUID((ref or "").strip())
    except (ValueError, AttributeError):
        pass
    found = _lookup(cur, ref, table=table, id_col=id_col, extra_where=extra_where)
    if not found:
        raise MashuError(f"no {label} begins with '{ref}'")
    if len(found) > 1:
        named = "  ".join(_short(value) for value in found)
        raise MashuError(f"'{ref}' names more than one {label}: {named}")
    return found[0]


def _memory_ref(cur: Any, ref: str) -> UUID:
    return _resolve(cur, ref, table="memory", id_col="memory_id", label="memory")


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


def _date(value: Any) -> str:
    return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]


def _field(label: str, value: Any) -> None:
    print(f"{label:<10}  {value}")


def _cells(text: str) -> int:
    """How wide a string prints, counting double-width characters as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _flow(text: str, indent: str = "  ", width: int | None = None) -> str:
    """Wrap a long body for reading, counting double-width characters as two.

    Not textwrap: that breaks at spaces, and much of what is stored here is
    Japanese, which has none. Measured in cells instead, a line breaks where
    the terminal would have wrapped it anyway — at a space when one is near,
    mid-run when there is none — and the indent marks where a record's body
    ends and the next record begins.
    """
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
                # A space is dropped only when it is the seam itself; carried
                # text keeps the spaces between its own words.
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


def _gate_warnings(result: dict[str, Any]) -> None:
    """Say when the entrance check did not run, or ran on a broken list.

    Both states let content through, which is also what a clean pass looks
    like, so nothing about the result distinguishes them. These go to stderr
    so a caller reading stdout for an id is unaffected, and the person at the
    terminal still sees that the gate was not what they assumed.
    """
    if result.get("unchecked"):
        print("warning: banned-pattern list not found; nothing was checked", file=sys.stderr)
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
    print(
        f"tokens  always={totals['always']}/{config.always_capacity()}  "
        f"worst={totals['worst']}/{config.capacity()}"
    )
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
        _gate_warnings(row)
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
    _gate_warnings(row)
    print(f"remembered  {row['memory_id']}")
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        memory_id = _memory_ref(cur, args.memory_id)
        row = memories.retire(cur, memory_id, reason=args.reason, actor=ACTOR)
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
    with db.transaction(args.dsn) as cur:
        row = ledger_domain.report_pain(
            cur,
            kind=args.kind,
            what=args.what,
            prevention=args.prevention,
            actor=ACTOR,
            scope_id=_scope(cur, args.scope),
        )
    _gate_warnings(row)
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
        print(
            f"{_short(row['ledger_id']):8}  {row['kind']:<9}  "
            f"{_date(row.get('created_at'))}  {row['what']}"
        )
        # The prevention is the sentence the matching runs on, so a listing
        # that hides it shows the half of each row that decides nothing.
        print(f"    prevention  {row['prevention']}")
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
    """The pains a row rests on, opened out.

    Ids alone answer nothing here. What makes a memory readable after the fact
    is the prevention sentence it was admitted against, which is also the
    sentence a later pain will collide with. The id still leads the line, so
    the ledger row itself can be opened from what is printed.
    """
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
        SELECT m.*, s.name AS scope_name
        FROM memory m LEFT JOIN scope s ON s.scope_id = m.scope_id
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
    delivery = row["delivery"]
    _field("delivery", f"guard  {row['guard_action']}" if delivery == "guard" else delivery)
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
    _field("scope", row["scope_name"] or "-")
    print("content")
    print(f"  {row['content']}")
    _print_evidence(cur, row["evidence"])
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
    # Which candidates and memories this pain was spent on. Without it the
    # ledger reads as a complaints file, and whether anything came of a pain
    # is exactly what a person reading one back wants to know.
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


_SHOW = {"memory": _show_memory, "nomination": _show_nomination, "ledger": _show_ledger}


def cmd_show(args: argparse.Namespace) -> int:
    """One row, whichever of the three tables it lives in, opened in full."""
    with db.transaction(args.dsn) as cur:
        found = [
            (table, value)
            for table, id_col in _REFERENCE_TABLES
            for value in _lookup(cur, args.ref, table=table, id_col=id_col)
        ]
        if not found:
            raise MashuError(f"nothing here answers to '{args.ref}'")
        if len(found) > 1:
            listed = "\n".join(f"  {table}  {_short(value)}" for table, value in found)
            raise MashuError(f"'{args.ref}' names more than one row:\n{listed}")
        table, value = found[0]
        _SHOW[table](cur, value)
    return 0


def cmd_memories(args: argparse.Namespace) -> int:
    status = "retired" if args.retired else "active"
    with db.transaction(args.dsn) as cur:
        scope_id = _scope(cur, args.scope) if args.scope else None
        cur.execute(
            """
            SELECT m.*, s.name AS scope_name
            FROM memory m LEFT JOIN scope s ON s.scope_id = m.scope_id
            WHERE m.status = %(status)s
              AND (%(scope)s::uuid IS NULL OR m.scope_id = %(scope)s::uuid)
            ORDER BY m.delivery, s.name, m.created_at
            """,
            {"status": status, "scope": scope_id},
        )
        rows = cur.fetchall()
    print("id        delivery  scope                 tokens  content")
    for row in rows:
        print(
            f"{_short(row['memory_id']):8}  {row['delivery']:<8}  "
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
        print(f"{row['nomination_id']}  {row['kind']}  {row.get('scope_name') or '-'}")
        print(f"  {row['content']}")
        if row.get("deferred_at"):
            print(f"  deferred: {row.get('defer_reason') or ''}")
        for evidence in row.get("evidence_rows", []):
            print(
                f"  evidence {evidence['kind']} {evidence.get('created_at', '')}: "
                f"{evidence['what']} / {evidence['prevention']}"
            )


def cmd_review(args: argparse.Namespace) -> int:
    if args.list:
        with db.transaction(args.dsn) as cur:
            rows = nominations.pending_nominations(cur, include_deferred=args.all)
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

    return review_ui.run(args.dsn, show_deferred=args.all)


def cmd_deliver(args: argparse.Namespace) -> int:
    with db.transaction(args.dsn) as cur:
        row = memories.set_delivery(
            cur,
            _memory_ref(cur, args.memory_id),
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
            memory_id = _memory_ref(cur, memory_ref)
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
            removed = routing.remove_route(cur, path_prefix=args.remove, actor=ACTOR)
            print("removed" if removed else "not found")
            return 0
        rows = routing.all_routes(cur)
    # A route is told apart by its tail, so a fixed column cutting the end
    # printed every path under one tree as the same row. The column is taken
    # from the longest route instead, measured in cells: a route through a
    # Japanese directory name spends two of them per character, and counting
    # characters left the scope beside it stepping in and out.
    header = "path_prefix"
    width = max([_cells(header), *(_cells(row["path_prefix"]) for row in rows)])
    print(f"{header}{' ' * (width - _cells(header))}  scope")
    for row in rows:
        prefix = row["path_prefix"]
        pad = " " * (width - _cells(prefix))
        print(f"{prefix}{pad}  {row.get('scope_name') or '(ignored)'}")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    for filename in migration.migrate(args.dsn):
        print(f"applied: {filename}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # The MCP client's own configuration is where the agent identity comes
    # from; --agent is the flag that configuration passes.
    if args.agent:
        os.environ["MASHU_AGENT"] = args.agent
    from mashu import server

    server.main()
    return 0


#: Said wherever an id is taken. Every listing prints a short id, so every
#: command that takes one has to accept the short id back.
_REF_HELP = "the row's id, or the first four or more characters of it"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mashu",
        description="The knowledge state that keeps only what forgetting has cost something.",
    )
    parser.add_argument("--dsn", default=None, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="stock, seats used, pending count, recent ledger").set_defaults(
        func=cmd_status
    )
    sub.add_parser(
        "bootstrap", help="what a session in this directory is pushed, and its token cost"
    ).set_defaults(func=cmd_bootstrap)

    remember = sub.add_parser(
        "remember", help="write a rule straight into the active set (the only immediate path)"
    )
    remember.add_argument("body", help="the rule, written as a short sentence")
    remember.add_argument("--scope", help="deliver it to this scope only")
    remember.add_argument(
        "--delivery",
        choices=("always", "scope", "guard"),
        help="where it is delivered (default: scope with --scope, otherwise always)",
    )
    remember.add_argument("--action", help="the tool the rule stands in front of, for guard")
    remember.add_argument(
        "--until", help="record a dated condition expiring in N days instead (at most 14)"
    )
    remember.set_defaults(func=cmd_remember)

    retire = sub.add_parser("retire", help="withdraw a memory, leaving the reason as its tombstone")
    retire.add_argument("memory_id", help=_REF_HELP)
    retire.add_argument("--reason", required=True, help="why it is wrong; later matches read this")
    retire.set_defaults(func=cmd_retire)

    revise = sub.add_parser("revise", help="rewrite a memory's body, keeping the old one on file")
    revise.add_argument("memory_id", help=_REF_HELP)
    revise.add_argument("--content", help="the new body; without it, an editor opens on the old")
    revise.set_defaults(func=cmd_revise)

    show = sub.add_parser("show", help="one memory, candidate, or ledger row in full")
    show.add_argument("ref", help=f"{_REF_HELP}, in any of the three tables")
    show.set_defaults(func=cmd_show)

    memories_parser = sub.add_parser("memories", help="list the memories held")
    memories_parser.add_argument("--scope", help="only the memories belonging to this scope")
    memories_parser.add_argument(
        "--retired", action="store_true", help="list the withdrawn ones, with their reasons"
    )
    memories_parser.set_defaults(func=cmd_memories)

    pain = sub.add_parser("pain", help="record a pain in the ledger and see what it resembles")
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
    pain.set_defaults(func=cmd_pain)

    ledger = sub.add_parser("ledger", help="read the pain ledger, newest first")
    ledger.add_argument("--limit", type=int, default=20, help="how many rows to show (default 20)")
    ledger.add_argument("--scope", help="only the pains recorded in this scope")
    ledger.set_defaults(func=cmd_ledger)

    trace = sub.add_parser("trace", help="read and search the traces (dated, unreviewed, 30 days)")
    trace.add_argument("query", nargs="?", help="text to match; without it, the recent traces")
    trace.add_argument("--scope", help="only the traces left in this scope")
    trace.set_defaults(func=cmd_trace)

    review = sub.add_parser("review", help="decide the pending candidates, one at a time")
    review_group = review.add_mutually_exclusive_group()
    review_group.add_argument("--list", action="store_true", help="print the queue and stop")
    review_group.add_argument("--admit", metavar="REF", help=f"admit one candidate: {_REF_HELP}")
    review_group.add_argument("--decline", metavar="REF", help=f"turn one down: {_REF_HELP}")
    review.add_argument(
        "--delivery",
        choices=("always", "scope", "guard"),
        help="where the admitted memory is delivered",
    )
    review.add_argument("--scope", help="the scope the admitted memory belongs to")
    review.add_argument("--action", help="the tool it stands in front of, for guard")
    review.add_argument("--reason", help="why it is turned down; required with --decline")
    review.add_argument(
        "--all", action="store_true", help="include the candidates that were put off"
    )
    review.set_defaults(func=cmd_review)

    deliver = sub.add_parser("deliver", help="move a memory between the opening and the act gate")
    deliver.add_argument("memory_id", help=_REF_HELP)
    deliver.add_argument(
        "delivery", choices=("always", "scope", "guard"), help="where it is delivered from now on"
    )
    deliver.add_argument("--action", help="the tool it stands in front of, for guard")
    deliver.add_argument("--scope", help="the scope it belongs to, for scope")
    deliver.set_defaults(func=cmd_deliver)

    guard = sub.add_parser("guard", help="the rules standing in front of one act")
    guard.add_argument("action", help="the tool being guarded")
    guard_group = guard.add_mutually_exclusive_group()
    guard_group.add_argument("--pin", metavar="REF", help=f"pin a memory here: {_REF_HELP}")
    guard_group.add_argument(
        "--unpin", metavar="REF", help=f"return a memory to the opening: {_REF_HELP}"
    )
    guard.add_argument("--json", action="store_true", help="print as JSON, for the hook to read")
    guard.set_defaults(func=cmd_guard)

    scope = sub.add_parser("scope", help="the scope register")
    scope.add_argument("--add", metavar="NAME", help="create a scope with this name")
    scope.add_argument("--about", metavar="LINE", help="what the scope covers; required with --add")
    scope.set_defaults(func=cmd_scope)

    route = sub.add_parser("route", help="which working directory means which scope")
    route_group = route.add_mutually_exclusive_group()
    route_group.add_argument("--add", metavar="PATH", help="route this path prefix to --scope")
    route_group.add_argument(
        "--ignore", metavar="PATH", help="record that this path prefix has no scope"
    )
    route_group.add_argument("--remove", metavar="PATH", help="drop the route for this path prefix")
    route.add_argument("--scope", help="the scope to route to; required with --add")
    route.set_defaults(func=cmd_route)

    serve = sub.add_parser("serve", help="run the MCP server on stdio")
    serve.add_argument("--agent", help="the name writes are attributed to")
    serve.set_defaults(func=cmd_serve)

    admin = sub.add_parser("admin", help="store maintenance")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    admin_sub.add_parser("migrate", help="apply the migrations not yet applied").set_defaults(
        func=cmd_migrate
    )
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
