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
import sys
import textwrap
from uuid import UUID

from mashu import proposals, retrieval, store
from mashu.db import transaction
from mashu.embed import get_embedder
from mashu.migrate import migrate
from mashu.models import MemoryType

WIDTH = 88


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text.strip(), width=WIDTH, initial_indent=indent, subsequent_indent=indent)


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
                print(
                    f"  {_short(item['proposal_id'])}  {item['operation']:<14} "
                    f"{item['memory_type'] or '-':<14} {item['title'] or ''}"
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
    """Everything about one proposal, including what it would replace."""
    with transaction(args.dsn) as cur:
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
            if entity["active_version"] and entity["active_version"] != version["version_id"]:
                current = store.get_version(cur, entity["active_version"])
                print("\ncurrently active:")
                print(_wrap(current["content"]))
        else:
            print("\npayload:")
            for key, value in proposal["payload"].items():
                print(f"    {key}: {value}")
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
    print(f"embedded {len(entities)} title(s) and {len(versions)} version(s)")
    return 0


def cmd_migrate(args) -> int:
    applied = migrate(args.dsn)
    print("\n".join(f"applied: {name}" for name in applied) or "already up to date")
    return 0


# --------------------------------------------------------------------------
# resolving abbreviated ids
# --------------------------------------------------------------------------
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

    s = sub.add_parser("show", help="show one proposal in full")
    s.add_argument("proposal_id")
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

    m = sub.add_parser("migrate", help="apply pending migrations")
    m.set_defaults(func=cmd_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
