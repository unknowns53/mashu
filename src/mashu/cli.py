"""The command line the user reviews from (specifications 18, 18.1).

Review is the only step of the loop a person has to perform, so what it costs
is what decides whether the whole thing is operable. Section 18.1 measured that
cost and found it is not the number of proposals but the number of times the
reader has to rebuild context, so the queue here is grouped into session
bundles and a bundle is approved in one command, with individual rejections
taken out of it.

Nothing in this module decides anything. It shows what is waiting and carries
the user's decision into the store.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import textwrap
from uuid import UUID

from mashu import bootstrap, db, importer, proposals, retrieval, scopes, server, store
from mashu.db import transaction
from mashu.embed import get_embedder
from mashu.errors import DeliveryError
from mashu.migrate import migrate
from mashu.models import Delivery, MemoryType, ProposalOperation, SourceType

WIDTH = 88


def _wrap(text: str, indent: str = "    ") -> str:
    """Wrap for reading, keeping the line breaks the writer put in.

    Filling the whole thing as one paragraph collapses a list of points into a
    wall, and the imported memories are mostly lists of points. The review
    reads these; making them harder to read to save a few lines would be the
    wrong trade in the one place where reading is the work.
    """
    out = []
    for line in text.strip().splitlines():
        if not line.strip():
            out.append("")
            continue
        hang = indent + "  " if line.lstrip().startswith(("-", "*", "•")) else indent
        out.append(
            textwrap.fill(line.strip(), width=WIDTH, initial_indent=indent, subsequent_indent=hang)
        )
    return "\n".join(out)


def _short(value: UUID | str) -> str:
    """Enough of an id to name it in a command without pasting all of it."""
    return str(value)[:8]


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------
def cmd_queue(args) -> int:
    """List what is waiting, one session bundle at a time."""
    with transaction(args.dsn) as cur:
        bundles = proposals.session_queue(cur)
        orphans = proposals.orphaned_candidates(cur)
        if not bundles:
            print("nothing waiting for review")
        else:
            total = sum(b["count"] for b in bundles)
            print(f"{total} proposal(s) in {len(bundles)} bundle(s)\n")
        for bundle in bundles:
            name = _short(bundle["session_id"]) if bundle["session_id"] else "no session"
            print(
                f"bundle {name}  {bundle['count']} proposal(s), "
                f"waiting {bundle['days_pending']} day(s)"
            )
            for item in bundle["proposals"]:
                # The scope is printed because it is the one decision review
                # cannot revise: nothing moves an entity between scopes, so a
                # reviewer who cannot see it cannot check it.
                print(
                    f"  {_short(item['proposal_id'])}  {item['operation']:<14} "
                    f"{item['memory_type'] or '-':<12} "
                    f"[{(item['scope_name'] or '-')[:14]:<14}] {item['title'] or ''}"
                )
            print()

        if orphans:
            print(
                f"warning: {len(orphans)} candidate version(s) have no pending "
                f"proposal. Agents see them tagged unreviewed, but no review "
                f"here can settle them."
            )
            for row in orphans:
                print(
                    f"  {_short(row['version_id'])}  {row['type']:<14} {row['title']}"
                    f"  ({row['days_pending']}d)"
                )
    return 0


def cmd_show(args) -> int:
    """Everything about one proposal, or a whole bundle read end to end.

    Section 18.1 makes the bundle the unit of review because the cost is not
    the number of proposals but the number of times the reader rebuilds
    context. Showing them one at a time puts that cost back: sixty-three
    invocations is sixty-three re-entries into the same subject. So a bundle
    prints as one document, in the order the section asks for, grounds before
    the conclusions drawn from them.
    """
    with transaction(args.dsn) as cur:
        if args.bundle:
            return _show_bundle(cur, args.bundle)
        proposal = _resolve_proposal(cur, args.proposal_id)
        print(f"proposal   {proposal['proposal_id']}")
        print(f"operation  {proposal['operation']}")
        print(f"actor      {proposal['actor']}")
        print(f"status     {proposal['status']}  ({proposal['decision_reason'] or 'no note'})")
        print(f"created    {proposal['created_at']:%Y-%m-%d %H:%M}")

        if proposal["applied_version"]:
            version = store.get_version(cur, proposal["applied_version"])
            entity = store.get_entity(cur, version["memory_id"])
            print(f"\nentity     {entity['title']}  [{entity['type']}, {entity['status']}]")
            print("\nproposed:")
            print(_wrap(version["content"]))
            _print_grounds(cur, version["version_id"])
            if entity["active_version"] and entity["active_version"] != version["version_id"]:
                current = store.get_version(cur, entity["active_version"])
                print("\ncurrently active:")
                print(_wrap(current["content"]))
        else:
            print("\npayload:")
            for key, value in proposal["payload"].items():
                print(f"    {key}: {value}")
    return 0


def _print_grounds(cur, version_id) -> None:
    """List what a version says it rests on (14, 19).

    A summary that may say only what its references say cannot be reviewed
    without them in front of the reader.
    """
    grounds = store.evidence_for(cur, version_id)
    if not grounds:
        return
    print(f"\nresting on ({len(grounds)}):")
    for row in grounds:
        standing = row["version_status"] or "nothing adopted yet"
        print(f"    {_short(row['memory_id'])}  [{row['type']}] {row['title']}  ({standing})")


def _show_bundle(cur, prefix: str) -> int:
    """One bundle printed as a single reading, with what each item would do."""
    session_id = None if prefix == "none" else _resolve_session(cur, prefix)
    bundle = next((b for b in proposals.session_queue(cur) if b["session_id"] == session_id), None)
    if bundle is None:
        print("no such bundle waiting for review")
        return 1

    scope = bundle["proposals"][0]["scope_name"] or "-"
    print(
        f"bundle {prefix}  [{scope}]  {bundle['count']} proposal(s), "
        f"waiting {bundle['days_pending']} day(s)\n"
    )

    for index, item in enumerate(bundle["proposals"], 1):
        print(f"{'-' * WIDTH}")
        print(
            f"{index:>3}/{bundle['count']}  {_short(item['proposal_id'])}  "
            f"{item['memory_type'] or '-'}  [{item['scope_name'] or '-'}]"
        )
        print(f"     {item['title'] or ''}\n")

        version_id = item.get("applied_version")
        if version_id is None:
            cur.execute(
                "SELECT applied_version FROM proposal WHERE proposal_id = %s",
                (item["proposal_id"],),
            )
            version_id = cur.fetchone()["applied_version"]
        if version_id is None:
            print(_wrap("(nothing written yet; this proposal waits before it changes anything)"))
            continue

        version = store.get_version(cur, version_id)
        if version["directive"]:
            print(_wrap(version["directive"], indent="  > "))
            print()
        print(_wrap(version["content"]))
        if version["source_reference"]:
            print(f"\n     from {version['source_reference']}")
    print(f"{'-' * WIDTH}")
    return 0


def cmd_approve(args) -> int:
    """Approve one proposal, or a whole bundle minus what is rejected."""
    with transaction(args.dsn) as cur:
        if args.bundle:
            session_id = None if args.bundle == "none" else _resolve_session(cur, args.bundle)
            skip = {_resolve_proposal(cur, p)["proposal_id"] for p in args.skip}
            done = proposals.approve_bundle(
                cur, session_id, reviewer=args.reviewer, reason=args.reason, skip=skip
            )
            print(f"approved {len(done)} proposal(s)")
            return 0
        proposal = _resolve_proposal(cur, args.proposal_id)
        proposals.approve(cur, proposal["proposal_id"], reviewer=args.reviewer, reason=args.reason)
        print(f"approved {proposal['proposal_id']}")
    return 0


def cmd_reject(args) -> int:
    """Turn one proposal down. The reason is required, and is kept."""
    with transaction(args.dsn) as cur:
        proposal = _resolve_proposal(cur, args.proposal_id)
        proposals.reject(cur, proposal["proposal_id"], reviewer=args.reviewer, reason=args.reason)
        print(f"rejected {proposal['proposal_id']}")
    return 0


# --------------------------------------------------------------------------
# reading and maintenance
# --------------------------------------------------------------------------
def cmd_search(args) -> int:
    """Run one query through the pipeline and print the three layers.

    Printing all three is the point rather than a convenience: it is how the
    user checks that retirement and semi-approval are landing where they
    should, which is what 27.1 measures.
    """
    with transaction(args.dsn) as cur:
        got = retrieval.retrieve(
            cur,
            args.query,
            actor=args.actor,
            types=[MemoryType(t) for t in args.type] if args.type else None,
            limit=args.limit,
        )
        print(f"scopes detected: {len(got.scopes)}  (searched: {len(got.narrowed_to)})\n")

        print(f"layer 1  active ({len(got.active)})")
        for row in got.active:
            print(f"  [{row['type']}] {row['title']}  ({row['similarity']:.3f})")
            print(_wrap(row["content"], indent="      "))

        print(f"\nlayer 2  unreviewed ({len(got.unreviewed)}, {got.dropped_unreviewed} dropped)")
        for row in got.unreviewed:
            print(
                f"  [{row['type']}] {row['title']}  ({row['similarity']:.3f}, "
                f"{row['days_pending']}d, {row['tag']})"
            )
            print(_wrap(row["content"], indent="      "))

        print(f"\nlayer 3  retired ({len(got.retired)})")
        for row in got.retired:
            print(f"  [{row['status']}] {row['title']}")
            print(_wrap(row["reason"] or "no reason recorded", indent="      "))
    return 0


def cmd_backfill(args) -> int:
    """Compute embeddings for rows written before the model was wired in.

    Also what section 27.1 needs: content imported from the native memory
    files arrives as text and is not findable until it has a vector.
    """
    embedder = get_embedder()
    print(f"embedding with {embedder.name}")
    with transaction(args.dsn) as cur:
        cur.execute("SELECT scope_id, name, description FROM scope WHERE name_embedding IS NULL")
        scopes = cur.fetchall()
        for row in scopes:
            cur.execute(
                "UPDATE scope SET name_embedding = %s WHERE scope_id = %s",
                (
                    store.embed_text(store.scope_description(row["name"], row["description"])),
                    row["scope_id"],
                ),
            )

        cur.execute("SELECT memory_id, title FROM memory_entity WHERE title_embedding IS NULL")
        entities = cur.fetchall()
        for row in entities:
            cur.execute(
                "UPDATE memory_entity SET title_embedding = %s WHERE memory_id = %s",
                (store.embed_text(row["title"]), row["memory_id"]),
            )

        cur.execute(
            "SELECT version_id, content FROM memory_version WHERE content_embedding IS NULL"
        )
        versions = cur.fetchall()
        for row in versions:
            cur.execute(
                "UPDATE memory_version SET content_embedding = %s WHERE version_id = %s",
                (store.embed_text(row["content"]), row["version_id"]),
            )
    print(
        f"embedded {len(scopes)} scope(s), {len(entities)} title(s) and {len(versions)} version(s)"
    )
    return 0


def cmd_import(args) -> int:
    """Bring a JSON file of memories into one scope as candidates (27.1)."""
    items = json.loads(pathlib.Path(args.file).read_text(encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("memories") or items.get("items") or []

    with transaction(args.dsn) as cur:
        scope_id = _scope_by_name(cur, args.scope)
        # One run is one bundle. Section 18.1 has the reviewer rebuild context
        # once per bundle, so leaving a whole migration unattached would make
        # every imported file part of a single unnamed pile whose size is the
        # size of the store.
        cur.execute(
            "INSERT INTO agent_session (agent) VALUES (%s) RETURNING session_id",
            (args.actor,),
        )
        session_id = cur.fetchone()["session_id"]
        summary = importer.import_items(
            cur, scope_id=scope_id, items=items, actor=args.actor, session_id=session_id
        )

    print(f"bundle {_short(session_id)}")

    print(f"{len(summary['created'])} new entity proposal(s)")
    for row in summary["created"]:
        print(f"  {_short(row['proposal_id'])}  {row['title']}")
    print(f"{len(summary['attached'])} attached to an existing entity")
    for row in summary["attached"]:
        print(
            f"  {_short(row['proposal_id'])}  {row['title']} -> {row['onto']}"
            f"  ({row['similarity']})"
        )
    if summary["failed"]:
        print(f"{len(summary['failed'])} could not be imported")
        for row in summary["failed"]:
            trouble = row.get("missing") or row.get("error")
            print(f"  item {row['index']}  {row['title']}: {trouble}")
    return 0


def cmd_scope(args) -> int:
    """Show what each scope declared it needs, and how far it has got (7.1)."""
    with transaction(args.dsn) as cur:
        if args.require:
            type_, requirement = args.require
            scopes.set_requirement(
                cur,
                scope_id=_scope_by_name(cur, args.name),
                type=MemoryType(type_),
                requirement=requirement,
                actor=args.actor,
            )
        if args.promote:
            try:
                opened = scopes.promote(
                    cur, scope_id=_scope_by_name(cur, args.name), actor=args.actor
                )
            except scopes.NotReadyError as refusal:
                print(f"not promoted: {refusal}")
                return 1
            print(f"{opened['name']} is now {opened['lifecycle']}\n")

        for row in scopes.readiness(cur):
            mark = "" if row["ready"] else f"  needs {', '.join(row['missing'])}"
            print(
                f"{row['name']:<16}{row['lifecycle']:<13}"
                f"adopted {row['adopted']:<4}unreviewed {row['pending']:<5}{mark}"
            )
            declared = [t for t, r in row["manifest"].items() if r == scopes.NOT_NEEDED]
            if declared:
                print(f"{'':<16}declared unnecessary: {', '.join(declared)}")
    return 0


def cmd_preview(args) -> int:
    """Rank one scope's candidates for a query, caps off (27.1 validation).

    Deliberately not what an agent sees. Retrieval hides the unreviewed layer
    when nothing is adopted to contrast it with, which is right for an agent
    and hides exactly what the migration has to check.
    """
    with transaction(args.dsn) as cur:
        rows = retrieval.preview(
            cur, args.query, scope_id=_scope_by_name(cur, args.scope), limit=args.limit
        )
        print(f"{len(rows)} candidate(s), ranked, no caps applied\n")
        for rank, row in enumerate(rows, 1):
            print(f"{rank:>3}. [{row['type']}] {row['title']}  ({row['similarity']:.3f})")
    return 0


def cmd_state(args) -> int:
    """Propose a scope's current state, with the memories it rests on (14, 19).

    A scope has one current state, so this adds a version to the one already
    there rather than putting a rival beside it. It is a proposal like any
    other: state is a candidate commit type (17), so what this writes is
    waiting for review, not in force.

    The references are the point. Section 14 lets the summary say only what its
    references already say, and without the edges that condition cannot be
    checked by anyone — which is how a current state turns back into the kind
    of summary prose this whole layer exists to stop importing.
    """
    content = pathlib.Path(args.file).read_text(encoding="utf-8").strip()
    if not content:
        raise SystemExit(f"{args.file} is empty")

    with transaction(args.dsn) as cur:
        scope_id = _scope_by_name(cur, args.scope)
        grounds = [_resolve_entity(cur, prefix)["memory_id"] for prefix in args.evidence]

        cur.execute(
            "SELECT memory_id, title, latest_version FROM memory_entity "
            "WHERE scope_id = %s AND type = 'state' AND status <> 'merged'",
            (scope_id,),
        )
        existing = cur.fetchall()
        if len(existing) > 1:
            listed = "\n".join(f"  {_short(r['memory_id'])}  {r['title']}" for r in existing)
            raise SystemExit(f"{args.scope} already has {len(existing)} states:\n{listed}")

        payload = {
            "content": content,
            "source_type": str(SourceType(args.source_type)),
            "source_reference": args.source_reference,
            "evidence": [str(g) for g in grounds],
        }
        if existing:
            target = existing[0]
            result = proposals.propose(
                cur,
                actor=args.actor,
                operation=ProposalOperation.UPDATE_VERSION,
                payload=payload,
                target_memory=target["memory_id"],
                based_on_version=target["latest_version"],
                allow_duplicate=args.anyway,
            )
            print(f"new version of {_short(target['memory_id'])}  {target['title']}")
        else:
            if not args.title:
                raise SystemExit(f"{args.scope} has no current state yet; pass --title")
            payload |= {
                "scope_id": str(scope_id),
                "type": str(MemoryType.STATE),
                "title": args.title,
            }
            result = proposals.propose(
                cur,
                actor=args.actor,
                operation=ProposalOperation.CREATE,
                payload=payload,
                allow_duplicate=args.anyway,
                allow_similar=args.anyway,
            )
            print(f"new current state for {args.scope}")

        proposal = result["proposal"]
        print(f"proposal {_short(proposal['proposal_id'])}  {proposal['status']}")
        print(f"  {result['ruling'].reason}")
        print(f"  resting on {len(grounds)} memory(ies)")
    return 0


def cmd_merge(args) -> int:
    """Fold one entity into another, keeping the history (20.2).

    Only the user runs this, which is why it is here and not on the agent's
    side of the wall. Section 20.1 sends a provisional entity either here or to
    active, and until now only one of those two doors was reachable.

    Which active version survives is asked rather than picked. The two entities
    hold two readings of one concept, and choosing between them is a judgement
    about which is true.
    """
    with transaction(args.dsn) as cur:
        source = _resolve_entity(cur, args.source)
        target = _resolve_entity(cur, args.into)
        keep = _keep_active(cur, args.keep_active, source, target)

        result = store.merge_entities(
            cur,
            source=source["memory_id"],
            target=target["memory_id"],
            actor=args.actor,
            reason=args.reason,
            keep_active=keep,
        )

    print(f"{source['title']}\n  merged into {target['title']}")
    print(f"  {result['versions_moved']} version(s) moved, {result['evidence_moved']} edge(s)")
    if result["not_chosen"]:
        print(f"  superseded {', '.join(_short(v) for v in result['not_chosen'])}")
    return 0


def _keep_active(cur, asked: str | None, source: dict, target: dict) -> UUID | None:
    """Which version stays active, named by version or by which side it is on.

    The words exist because the answer is usually known before the versions
    are: a merge is planned while the thing being merged is still in review,
    and a command that cannot be written until then is a command written under
    pressure.
    """
    if asked is None:
        return None
    if asked in ("source", "into"):
        row = source if asked == "source" else target
        cur.execute(
            "SELECT active_version FROM memory_entity WHERE memory_id = %s", (row["memory_id"],)
        )
        active = cur.fetchone()["active_version"]
        if active is None:
            raise SystemExit(f"the {asked} entity has no active version to keep")
        return active
    return _resolve_version(cur, asked)


def _resolve_version(cur, prefix: str) -> UUID:
    cur.execute(
        "SELECT version_id FROM memory_version WHERE version_id::text LIKE %s",
        (f"{prefix}%",),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"no version starting {prefix!r}")
    if len(rows) > 1:
        raise SystemExit(f"{prefix!r} matches {len(rows)} versions; use more characters")
    return rows[0]["version_id"]


def cmd_evidence(args) -> int:
    """Both directions of one memory's reference edges (19)."""
    with transaction(args.dsn) as cur:
        entity = _resolve_entity(cur, args.memory)
        cur.execute(
            "SELECT active_version, latest_version FROM memory_entity WHERE memory_id = %s",
            (entity["memory_id"],),
        )
        row = cur.fetchone()
        # A memory under review has no active version, and its grounds are
        # exactly what the reviewer needs; falling back to the latest is not a
        # loosening, because this reads edges and never content.
        reading = row["active_version"] or row["latest_version"]
        which = "active" if row["active_version"] else "awaiting review"

        print(f"{_short(entity['memory_id'])}  {entity['title']}\n")

        grounds = store.evidence_for(cur, reading) if reading else []
        print(f"rests on ({len(grounds)}, read from the {which} version)")
        for ground in grounds:
            standing = ground["version_status"] or "nothing adopted yet"
            print(
                f"  {_short(ground['memory_id'])}  [{ground['type']}] "
                f"{ground['title']}  ({standing})"
            )

        dependants = store.resting_on(cur, entity["memory_id"])
        print(f"\nsupports ({len(dependants)})")
        for row in dependants:
            live = "active" if row["is_active"] else row["status"]
            print(f"  {_short(row['memory_id'])}  [{row['type']}] {row['title']}  ({live})")
    return 0


def cmd_bootstrap(args) -> int:
    """Show what a session start is handed, and what it costs (21.2)."""
    with transaction(args.dsn) as cur:
        got = bootstrap.session_bootstrap(
            cur,
            actor=args.actor,
            scopes=[_scope_by_name(cur, args.scope)] if args.scope else None,
            record_event=False,
        )

    print(f"scope index ({len(got.scope_index)})")
    for row in got.scope_index:
        summary = f"  {row['summary']}" if row["summary"] else ""
        print(f"  {str(row['scope_id'])[:8]}  {row['name']}{summary}")

    for label, rows in (("pushed at session start", got.startup), ("for this scope", got.scoped)):
        print(f"\n{label} ({len(rows)})")
        for row in rows:
            body = row["content"] if row["content"] is not None else "(trimmed; use memory_get)"
            print(f"  {str(row['memory_id'])[:8]}  {row['title']}")
            print(f"      {body}")

    over = " over the ceiling" if got.over_budget else ""
    print(f"\n{got.tokens} token of {bootstrap.BOOTSTRAP_TOKEN_BUDGET}{over}")
    if got.trimmed:
        print(f"{len(got.trimmed)} item(s) had their content dropped to fit")
    return 0


def cmd_deliver(args) -> int:
    """Move one memory between push and pull (21.2). The user's call only."""
    with transaction(args.dsn) as cur:
        entity = _resolve_entity(cur, args.memory)
        try:
            changed = store.set_delivery(
                cur,
                memory_id=entity["memory_id"],
                delivery=Delivery(args.delivery),
                actor=args.actor,
            )
        except DeliveryError as refusal:
            print(f"not changed: {refusal}")
            return 1
    print(f"{changed['title']}  ->  {changed['delivery']}")
    return 0


def cmd_serve(args) -> int:
    """Run the MCP server on stdin and stdout, which is how a CLI starts it."""
    if args.dsn:
        os.environ[db.DSN_ENV_VAR] = args.dsn
    if args.agent:
        os.environ[server.ACTOR_ENV_VAR] = args.agent
    server.build_server().run()
    return 0


def cmd_migrate(args) -> int:
    applied = migrate(args.dsn)
    print("\n".join(f"applied: {name}" for name in applied) or "already up to date")
    return 0


# --------------------------------------------------------------------------
# resolving abbreviated ids
# --------------------------------------------------------------------------
def _resolve_entity(cur, prefix: str) -> dict:
    """One entity by the start of its id, refusing to guess between two."""
    cur.execute(
        "SELECT memory_id, title, delivery FROM memory_entity WHERE memory_id::text LIKE %s",
        (f"{prefix}%",),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"no memory starting {prefix!r}")
    if len(rows) > 1:
        listed = "\n".join(f"  {_short(r['memory_id'])}  {r['title']}" for r in rows)
        raise SystemExit(f"{prefix!r} matches {len(rows)} memories:\n{listed}")
    return rows[0]


def _scope_by_name(cur, name: str) -> UUID:
    """A scope by its name. Scopes are the user's to create, so a miss is fatal."""
    cur.execute("SELECT scope_id FROM scope WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"no scope named {name!r}; the user creates scopes")
    return row["scope_id"]


def _resolve_proposal(cur, prefix: str) -> dict:
    cur.execute(
        "SELECT * FROM proposal WHERE proposal_id::text LIKE %s ORDER BY created_at",
        (prefix + "%",),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"no proposal starts with {prefix}")
    if len(rows) > 1:
        raise SystemExit(f"{prefix} matches {len(rows)} proposals; use more characters")
    return rows[0]


def _resolve_session(cur, prefix: str) -> UUID:
    cur.execute(
        "SELECT session_id FROM agent_session WHERE session_id::text LIKE %s",
        (prefix + "%",),
    )
    rows = cur.fetchall()
    if not rows:
        raise SystemExit(f"no session starts with {prefix}")
    if len(rows) > 1:
        raise SystemExit(f"{prefix} matches {len(rows)} sessions; use more characters")
    return rows[0]["session_id"]


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mashu", description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=None, help="database connection string")
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("queue", help="list proposals waiting for review, by session")
    q.set_defaults(func=cmd_queue)

    s = sub.add_parser("show", help="show one proposal, or a whole bundle, in full")
    s.add_argument("proposal_id", nargs="?")
    s.add_argument("--bundle", help="session id prefix, or 'none', to read the whole bundle")
    s.set_defaults(func=cmd_show)

    a = sub.add_parser("approve", help="approve a proposal or a whole bundle")
    a.add_argument("proposal_id", nargs="?")
    a.add_argument("--bundle", help="session id prefix, or 'none' for the unattributed bundle")
    a.add_argument("--skip", nargs="*", default=[], help="proposal ids to leave out")
    a.add_argument("--reviewer", default="user")
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_approve)

    r = sub.add_parser("reject", help="turn a proposal down, with a reason")
    r.add_argument("proposal_id")
    r.add_argument("--reason", required=True)
    r.add_argument("--reviewer", default="user")
    r.set_defaults(func=cmd_reject)

    f = sub.add_parser("search", help="run a query through the retrieval pipeline")
    f.add_argument("query")
    f.add_argument("--type", action="append", help="restrict to a memory type")
    f.add_argument("--limit", type=int, default=retrieval.DEFAULT_LIMIT)
    f.add_argument("--actor", default="user")
    f.set_defaults(func=cmd_search)

    b = sub.add_parser("backfill", help="embed rows that have no vector yet")
    b.set_defaults(func=cmd_backfill)

    i = sub.add_parser("import", help="import a JSON file of memories into a scope")
    i.add_argument("file")
    i.add_argument("--scope", required=True, help="name of an existing scope")
    i.add_argument("--actor", default="import")
    i.set_defaults(func=cmd_import)

    m = sub.add_parser("migrate", help="apply pending migrations")
    m.set_defaults(func=cmd_migrate)

    n = sub.add_parser("bootstrap", help="show the fixed context a session starts with")
    n.add_argument("--scope", default=None, help="narrow the current state to one scope")
    n.add_argument("--actor", default="user")
    n.set_defaults(func=cmd_bootstrap)

    c = sub.add_parser("scope", help="scope readiness and lifecycle")
    c.add_argument("name", nargs="?", default=None)
    c.add_argument("--promote", action="store_true", help="open the scope for use")
    c.add_argument(
        "--require",
        nargs=2,
        metavar=("TYPE", "REQUIREMENT"),
        default=None,
        help="declare a type required or not_needed for this scope",
    )
    c.add_argument("--actor", default="user")
    c.set_defaults(func=cmd_scope)

    t = sub.add_parser("state", help="propose a scope's current state (14)")
    t.add_argument("file", help="file holding the summary")
    t.add_argument("--scope", required=True, help="name of an existing scope")
    t.add_argument("--title", help="required when the scope has no current state yet")
    t.add_argument(
        "--evidence", nargs="*", default=[], metavar="ID", help="memories the summary rests on"
    )
    t.add_argument("--source-type", default="agent", choices=[str(s) for s in SourceType])
    t.add_argument("--source-reference", default=None)
    t.add_argument("--actor", default="claude")
    t.add_argument(
        "--anyway",
        action="store_true",
        help="propose even though an equivalent one is already waiting (15.1)",
    )
    t.set_defaults(func=cmd_state)

    mg = sub.add_parser("merge", help="fold one entity into another (20.2)")
    mg.add_argument("source", help="the entity that stops being separate")
    mg.add_argument("--into", required=True, help="the entity it becomes part of")
    mg.add_argument(
        "--keep-active",
        metavar="WHICH",
        help="which version stays active: 'source', 'into', or a version id",
    )
    mg.add_argument("--reason", required=True)
    mg.add_argument("--actor", default="user")
    mg.set_defaults(func=cmd_merge)

    ev = sub.add_parser("evidence", help="what a memory rests on, and what rests on it (19)")
    ev.add_argument("memory")
    ev.set_defaults(func=cmd_evidence)

    w = sub.add_parser("preview", help="rank a scope's candidates, caps off (27.1)")
    w.add_argument("query")
    w.add_argument("--scope", required=True)
    w.add_argument("--limit", type=int, default=retrieval.DEFAULT_LIMIT)
    w.set_defaults(func=cmd_preview)

    d = sub.add_parser("deliver", help="move a memory between push and pull (21.2)")
    d.add_argument("memory", help="memory id, or enough of its start to be unambiguous")
    d.add_argument("delivery", choices=[str(x) for x in Delivery])
    d.add_argument("--actor", default="user")
    d.set_defaults(func=cmd_deliver)

    v = sub.add_parser("serve", help="run the MCP server over stdio")
    v.add_argument("--agent", default=None, help="the identity proposals are recorded under")
    v.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
