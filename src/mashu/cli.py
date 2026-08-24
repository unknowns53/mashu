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
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from uuid import UUID

try:  # the review reads single keystrokes, which needs a posix terminal
    import termios
    import tty
except ImportError:  # pragma: no cover - no such terminal here
    termios = None
    tty = None

from mashu import (
    bootstrap,
    context,
    db,
    extract,
    importer,
    incidents,
    metrics,
    proposals,
    resolution,
    retrieval,
    routing,
    runs,
    server,
    store,
    thresholds,
    transcript,
    worker,
)
from mashu.db import transaction
from mashu.embed import get_embedder
from mashu.errors import DeliveryError, DuplicateProposalError, MashuError
from mashu.incidents import LEADS_TO
from mashu.migrate import migrate
from mashu.models import (
    Delivery,
    EntityStatus,
    IncidentCause,
    MemoryType,
    ProposalOperation,
    ProposalStatus,
    SourceType,
    VersionStatus,
)

WIDTH = 88


def _across() -> int:
    """The width to lay text out at: the terminal's, but never wider than reads well."""
    return min(WIDTH, _width())


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
            textwrap.fill(
                line.strip(), width=_across(), initial_indent=indent, subsequent_indent=hang
            )
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


def _grounds_text(cur, version_id) -> str:
    """What a version says it rests on (14, 19).

    A summary that may say only what its references say cannot be reviewed
    without them in front of the reader.
    """
    grounds = store.evidence_for(cur, version_id)
    if not grounds:
        return ""
    many = "memory" if len(grounds) == 1 else "memories"
    lines = [
        f"\n  built on {len(grounds)} other {many}. deciding this one does not decide them (19):"
    ]
    for row in grounds:
        lines.append(f"    [{row['type']}] {row['title']}")
        lines.append(f"      {_standing_of(row)}   ·   mashu inspect {_short(row['memory_id'])}")
    return "\n".join(lines)


#: What a ground's standing means for the reader deciding on top of it. The
#: status names are the store's vocabulary and say nothing on their own to
#: someone who is being asked a question about this proposal, not about that one.
_STANDING = {
    str(VersionStatus.CANDIDATE): "not adopted yet: its own review has not happened",
    str(VersionStatus.DISPROVEN): "disproven: what this is built on was found to be wrong",
    str(VersionStatus.COMPLETED): "completed: what this is built on is finished",
    str(VersionStatus.DORMANT): "set aside: out of use, and open to being taken up again",
    str(VersionStatus.REJECTED): "turned down at review",
    str(VersionStatus.SUPERSEDED): "replaced by a newer reading",
}


def _standing_of(row) -> str:
    """Where a ground stands, said as what it means rather than as a status name."""
    if row["entity_status"] == str(EntityStatus.MERGED):
        return "folded into another memory"
    if row["version_status"] is not None:
        if row["entity_status"] == str(EntityStatus.PROVISIONAL):
            return "adopted, but the entity is provisional and stays out of layer 1"
        return "adopted: standing knowledge"
    return _STANDING.get(row["latest_status"], "not adopted: nothing of it is standing")


def _look_alike_text(near: list[dict]) -> str:
    """What else in the scope already says something close to this (20).

    Written next to the grounds because the two answer the reader's two
    standing questions about a proposal: what it is built on, and whether the
    store already holds it.
    """
    if not near:
        return ""
    many = "one other" if len(near) == 1 else f"{len(near)} others"
    lines = [f"\n  looks like {many} in this scope. one concept, one entity (20):"]
    for row in near:
        close = "the same title" if row["same_title"] else f"{row['similarity']:.2f}"
        lines.append(f"    {close:>14}  [{row['type']}] {_clip(row['title'], 50)}")
        lines.append(
            f"          {_standing_of(row)}   ·   mashu inspect {_short(row['memory_id'])}"
        )
    return "\n".join(lines)


def _print_grounds(cur, version_id) -> None:
    text = _grounds_text(cur, version_id)
    if text:
        print(text)


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
        print(f"declined {proposal['proposal_id']}")
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
            note = _pending_note(row)
            if note:
                print(_wrap(note, indent="      "))
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
    """What scopes there are, and how much of each has been through review.

    Nothing here gates anything. The lifecycle and readiness manifest that used
    to live here were withdrawn in v0.11: with the relative cap gone, a scope
    with nothing adopted answers like any other, so declaring it open stopped
    meaning anything and the declaring was work without a judgement in it.

    --add is where a scope comes from. Section 7 has always said only the user
    creates one, and until now that was true by there being no way at all: an
    agent could not, and neither could the person the rule reserves it for. A
    session run somewhere unmapped is held for a scope that cannot be made
    (16.3), so the ledger's one human-only write had no entrance.

    The description is not decoration. Scope detection matches a query against
    the name and this line (27.2), and the index in every session opening is
    this line, so a scope without one is reachable only by someone who already
    knows it is there.
    """
    with transaction(args.dsn) as cur:
        if args.add:
            scope_id = store.create_scope(
                cur, name=args.add, description=args.about, actor=args.actor
            )
            print(f"{args.add}  {_short(scope_id)}")
            if not args.about:
                print("  no description; scope detection has only the name to match on")
            if args.route:
                row = routing.add(
                    cur, path_prefix=args.route, scope_id=scope_id, created_by=args.actor
                )
                released = runs.release(cur, cwd_prefix=row["path_prefix"])
                print(f"  {row['path_prefix']}  ->  {args.add}")
                if released:
                    print(f"  released {released} held transcript(s) back into the queue")
            return 0

        cur.execute(
            """
            SELECT s.name,
                   count(e.memory_id) AS entities,
                   count(e.active_version) AS adopted
            FROM scope s
            LEFT JOIN memory_entity e ON e.scope_id = s.scope_id AND e.status <> 'merged'
            WHERE s.status = 'active'
            GROUP BY s.name
            ORDER BY s.name
            """
        )
        for row in cur.fetchall():
            waiting = row["entities"] - row["adopted"]
            print(f"{row['name']:<16}adopted {row['adopted']:<5}unreviewed {waiting}")
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


def cmd_active(args) -> int:
    """Everything a scope holds as true, whole (16.1, 27.4b).

    This is the second input to session end extraction. Retirement detection
    reads every standing memory against what the session observed, so unlike
    every other read here it ranks nothing and hides nothing: a memory nobody
    thought to search for is the one that goes on being wrong.

    The json form is what gets handed to the prompt. The plain form is for a
    person checking what is about to be handed over.
    """
    with transaction(args.dsn) as cur:
        rows = retrieval.active_set(
            cur, scope_id=_scope_by_name(cur, args.scope) if args.scope else None
        )

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "memory_id": str(row["memory_id"]),
                        "type": row["type"],
                        "title": row["title"],
                        "content": row["content"],
                    }
                    for row in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"{len(rows)} active memory(ies)\n")
    for row in rows:
        print(f"{_short(row['memory_id'])}  [{row['type']}] {row['title']}")
        if args.full:
            print(_wrap(row["content"]))
            print()
    return 0


def cmd_remember(args) -> int:
    """Record something the user states, which lands straight away (16, 17).

    Section 16 names this trigger — the user says to remember something — and
    section 17 puts it on the auto commit line whatever its type, because the
    source is what makes it safe. Nothing here reached the command line before,
    so the one path that does not need a review was the one with no way in.

    An existing entity with a close enough title stops this rather than being
    written alongside. Which of the two it is — a second memory or a new
    version of the first — is a judgement, and the layer does not make it.
    """
    content = pathlib.Path(args.file).read_text(encoding="utf-8") if args.file else args.content
    content = (content or "").strip()
    if not content:
        raise SystemExit("nothing to remember; pass content or --file")

    if args.until:
        return _remember_until(args, content)

    missing = [f for f in ("scope", "type", "title") if not getattr(args, f)]
    if missing:
        raise SystemExit(
            f"a memory needs --{', --'.join(missing)}. For something that expires, "
            f"pass --until instead and it takes none of them (25.2)"
        )

    with transaction(args.dsn) as cur:
        try:
            result = proposals.propose(
                cur,
                actor=args.actor,
                operation=ProposalOperation.CREATE,
                payload={
                    "scope_id": str(_scope_by_name(cur, args.scope)),
                    "type": str(MemoryType(args.type)),
                    "title": args.title,
                    "content": content,
                    "directive": args.directive,
                    "source_type": str(SourceType.USER),
                },
                allow_similar=args.anyway,
            )
        except resolution.SimilarEntityError as clash:
            listed = "\n".join(
                f"  {_short(row['memory_id'])}  {row['title']}  ({row['similarity']:.3f})"
                for row in clash.candidates
            )
            raise SystemExit(
                f"{clash}\n{listed}\n"
                f"add a version to one of those, or pass --anyway to record this beside them"
            ) from clash

        proposal = result["proposal"]
        entity = store.get_entity(cur, proposal["target_memory"])
        landed = entity["active_version"] is not None

    print(f"{_short(entity['memory_id'])}  [{entity['type']}] {args.title}")
    print(f"  {'active' if landed else proposal['status']}  ({result['ruling'].reason})")
    return 0


def cmd_retype(args) -> int:
    """Correct what kind of thing an entity is (8).

    The user runs it directly, the way a merge is run directly: the judgement
    it needs — is this a decision or an observation — is the user's, and a
    proposal from an agent asking for it waits for them anyway (17).

    No version is created. The content does not change, and putting an
    identical body in the history would make a correction look like a change of
    mind.
    """
    with transaction(args.dsn) as cur:
        entity = _resolve_entity(cur, args.memory)
        moved = store.set_type(
            cur,
            memory_id=entity["memory_id"],
            target=MemoryType(args.to),
            actor=args.actor,
            reason=args.reason,
        )
    print(f"{moved['title']}\n  {moved['from']} -> {moved['to']}")
    return 0


def cmd_retitle(args) -> int:
    """Correct what an entity is called (8, 14).

    The user runs it directly, for the reason retype is run directly: naming is
    a judgement, and an agent asking for it waits for a person anyway (17).

    A Current State is what needs it. Its body is rewritten as the work moves
    and its title stayed where the first version left it, so the session
    opening announced a state the entity itself had stopped holding.

    No version is created. The content does not change.
    """
    with transaction(args.dsn) as cur:
        entity = _resolve_entity(cur, args.memory)
        moved = store.set_title(
            cur,
            memory_id=entity["memory_id"],
            title=args.to,
            actor=args.actor,
            reason=args.reason,
        )
    print(f"{moved['from']}\n  ->  {moved['to']}")
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


def _remember_until(args, content: str) -> int:
    """Record something that stops applying at a stated moment (25.2).

    Not a Memory, so it takes no type, no title and no review. What the user
    states here they also authorised the end of, at the moment they said it.
    """
    when = _when(args.until)
    with transaction(args.dsn) as cur:
        row = context.put(
            cur,
            content=content,
            expires_at=when,
            kind=args.kind,
            source_type=SourceType.USER,
            created_by=args.actor,
            actor=args.actor,
            scope_id=_scope_by_name(cur, args.scope) if args.scope else None,
        )
    where = args.scope or "every scope"
    print(f"{_short(row['context_id'])}  [{row['kind']}] until {_moment(row['expires_at'])}")
    print(f"  {where}; it leaves on its own, no review")
    return 0


def _moment(when) -> str:
    """A moment with its offset, always.

    25.2 refuses an expiry that reads differently to different readers. Printing
    one without its zone reintroduces on the way out exactly what the input
    rule keeps out.
    """
    return f"{when:%Y-%m-%d %H:%M %z}".replace(" +", " UTC+").replace(" -", " UTC-")


def _when(text: str):
    """A moment, from an ISO timestamp or a plain duration.

    Only what resolves the same way twice. Anything looser — tomorrow morning,
    the end of the month — is read differently by different readers, and 25.2
    would rather send it to scratch than guess.
    """
    import datetime as _dt
    import re as _re

    now = _dt.datetime.now().astimezone()
    match = _re.fullmatch(r"(\d+)\s*(h|hour|hours|d|day|days|w|week|weeks)", text.strip())
    if match:
        n = int(match.group(1))
        unit = match.group(2)[0]
        return now + _dt.timedelta(hours=n if unit == "h" else n * 24 * (7 if unit == "w" else 1))
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(
            f"cannot read {text!r} as a moment. Give an ISO timestamp, or a duration "
            f"like 24h, 3d, 2w (25.2 takes only what resolves the same way twice)"
        ) from None
    return parsed if parsed.tzinfo else parsed.astimezone()


def cmd_enqueue(args) -> int:
    """Claim a transcript for extraction. This is all a session-end hook does.

    The hook runs inside the CLI's exit path, which is measured in seconds, so
    it must not call a model, open a network connection, or wait on anything.
    It writes one row and returns. Everything expensive happens later, in a
    worker that nothing is waiting for.

    The session id and the working directory are read from the file's opening
    records when the hook did not supply them, which is a few hundred lines
    rather than the whole transcript. The directory has to be recorded here
    because it is what decides the scope later, and by then the process that
    knew it has exited.

    Failing here must not fail the hook: a session that cannot be enqueued is a
    session the sweeper will find by walking transcripts, and a non-zero exit
    from a hook is a visible error for something the user did not ask for.
    """
    path = pathlib.Path(args.transcript)
    if not path.exists():
        print(f"no transcript at {path}", file=sys.stderr)
        return 0

    try:
        peeked = transcript.peek(path, source_cli=args.cli)
        digest = transcript.digest(path)
        session_id = args.session or peeked.external_id
        if not session_id:
            print("no session id given and none in the transcript", file=sys.stderr)
            return 0
        with transaction(args.dsn) as cur:
            run = runs.enqueue(
                cur,
                source_cli=args.cli or peeked.source_cli,
                external_session_id=session_id,
                transcript_digest=digest,
                extractor_version=args.extractor_version,
                transcript_path=str(path.resolve()),
                cwd=args.cwd or peeked.cwd,
            )
    except Exception as failure:  # noqa: BLE001 - a hook must not break the CLI it runs in
        print(f"could not enqueue: {failure}", file=sys.stderr)
        return 0

    print(f"{_short(run['run_id'])}  {run['state']}")
    return 0


def cmd_runs(args) -> int:
    """Whether automatic capture is still working (16.3)."""
    with transaction(args.dsn) as cur:
        state = runs.health(cur)
        cur.execute(
            "SELECT source_cli, external_session_id, state, attempts, last_error, created_at "
            "FROM extraction_run WHERE state IN ('failed', 'retrying') ORDER BY created_at LIMIT 20"
        )
        stuck = cur.fetchall()

    print(
        f"succeeded {state['succeeded']}  skipped {state['skipped']}  "
        f"waiting {state['waiting']}  failed {state['failed']}"
    )
    if state["oldest_wait_hours"] is not None:
        print(f"oldest wait  {state['oldest_wait_hours']}h")
    if state["warning"]:
        print(f"\n{state['warning']}")
    for row in stuck:
        print(
            f"\n  {row['state']:<9}{row['source_cli']}/{row['external_session_id'][:8]}"
            f"  attempt {row['attempts']}"
        )
        if row["last_error"]:
            print(f"    {row['last_error'][:140]}")
    return 0


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
            print(f"  {_short(ground['memory_id'])}  [{ground['type']}] {ground['title']}")
            print(f"    {_standing_of(ground)}")

        dependants = store.resting_on(cur, entity["memory_id"])
        print(f"\nsupports ({len(dependants)})")
        for row in dependants:
            live = "active" if row["is_active"] else row["status"]
            print(f"  {_short(row['memory_id'])}  [{row['type']}] {row['title']}  ({live})")
    return 0


def _pending_note(row) -> str | None:
    """What is already proposed against a row being handed over (21.1).

    Both halves of that section's annotation. The retirement note came first;
    the update note is the one that matters for anything pushed, because what
    is pushed is the adopted reading and a Current State goes out of date in
    hours while a review takes days.
    """
    if row.get("proposed_status"):
        return (
            f"! {row['proposed_by']} has proposed {row['proposed_status']}: "
            f"{row['proposed_reason'] or 'no reason given'}"
        )
    if row.get("update_proposed_by"):
        days = row.get("update_proposed_days")
        waited = f", waiting {days}d" if days else ""
        return (
            f"! a newer version is written and unreviewed "
            f"(by {row['update_proposed_by']}{waited}); this is the adopted one"
        )
    return None


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
            note = _pending_note(row)
            if note:
                print(_wrap(note, indent="      "))
            print(f"      {body}")

    over = " over the ceiling" if got.over_budget else ""
    print(f"\n{got.tokens} token of {bootstrap.BOOTSTRAP_TOKEN_BUDGET}{over}")
    if got.trimmed:
        print(f"{len(got.trimmed)} item(s) had their content dropped to fit")
    return 0


def cmd_deliver(args) -> int:
    """Move one memory between push and pull (21.2). The user's call only."""
    try:
        with transaction(args.dsn) as cur:
            entity = _resolve_entity(cur, args.memory)
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


def cmd_status(args) -> int:
    """The indicators of 27.3, and the ones with nowhere to read from (30 段 C).

    Written to be read in one pass while deciding whether to sit down for a
    review, so the order is: is capture alive, how far behind is the queue, how
    much of the store has never been decided on, and what every session is
    paying before it asks anything.

    The last block is the one that earns the command. Section 27.3 names
    indicators the system cannot yet answer, and a status page that showed only
    the answerable ones would read as a full account of a system half of whose
    failure modes nothing is watching.
    """
    with transaction(args.dsn) as cur:
        got = metrics.collect(cur, window_days=args.days)

    health = got.capture
    print("capture (16.3)")
    print(
        f"  succeeded {health['succeeded']}   waiting {health['waiting']}   "
        f"failed {health['failed']}   held {health['held']}   skipped {health['skipped']}"
    )
    if health.get("warning"):
        print(_wrap(health["warning"], indent="  ! "))

    q, lat = got.queue, got.latency
    print(f"\nreview load (27.3, last {args.days} day(s))")
    put_off = f", {q['deferred']} put off" if q["deferred"] else ""
    print(f"  waiting        {q['proposals']} proposal(s) in {q['bundles']} bundle(s){put_off}")
    print(f"  oldest wait    {q['oldest_days']} day(s)")
    print(f"  decided        {lat['decided']} in {lat['bundles']} bundle(s)")
    print(f"  bundles/day    {lat['bundles_per_day']}")
    print(f"  time to decide {_span(lat['median_s'])} median, {_span(lat['p90_s'])} p90")
    if got.review.get("warning"):
        print(_wrap(got.review["warning"], indent="  ! "))

    print("\nunreviewed share (27.3; measured on the store, not on what retrieval returned)")
    for row in got.unreviewed:
        print(
            f"  {row['name']:<14}{row['share']:>5.0%}   "
            f"({row['unreviewed']} of {row['held']} never decided)"
        )

    tag = got.tag_share
    print(f"\ntag share (27.3; {metrics.TAG_SHARE_CEILING:.0%} sustained is the signal)")
    if not tag["assemblies"]:
        print("  nothing retrieved in the window")
    else:
        median = f"{tag['median']:.0%}" if tag["median"] is not None else "-"
        flag = "  over the ceiling" if tag["over_ceiling"] else ""
        print(f"  median {median} over {tag['assemblies']} assembly(s){flag}")
        print(f"  carried an unreviewed item: {tag['with_any_unreviewed']}")

    print("\nsession opening (21.2)")
    for row in got.openings:
        note = f"   {row['trimmed']} trimmed" if row["trimmed"] else ""
        print(
            f"  {row['name']:<14}{row['cost']:>5} of {row['budget']}   "
            f"{row['startup']}+{row['scoped']} pushed{note}"
        )

    c = got.corrections
    print("\ncorrections (27.3; counted, not judged — no baseline exists yet)")
    per = "-" if c["per_100_retrievals"] is None else c["per_100_retrievals"]
    print(
        f"  {c['declined']} turned down, {c['retired_by_hand']} retired by hand, "
        f"over {c['retrievals']} retrieval(s) = {per} per 100"
    )

    print("\nself-dating actives (25.2 移行)")
    if not got.self_dating:
        print("  none; no adopted rule names a moment in itself")
    else:
        pushed = sum(1 for row in got.self_dating if row["delivery"] != str(Delivery.PULL_ONLY))
        print(
            f"  {len(got.self_dating)} rule(s) name a moment in themselves"
            f"{f', {pushed} of them pushed' if pushed else ''} — 'mashu admin stale'"
        )

    print("\nnot measured here")
    for line in got.unmeasured:
        print(_wrap(line, indent="  - "))
    return 0


def cmd_thresholds(args) -> int:
    """Measure the similarity distributions rather than guessing them (27.2).

    Kept runnable rather than run once. The scope detection floor sits about
    0.04 above the median unrelated pair, and that margin is a property of the
    embedding model and of what the store happens to hold — both of which move.
    A number measured once and never again is a guess with a date on it.
    """
    thresholds.main(args.dsn)
    return 0


def cmd_eval_retire(args) -> int:
    """Does the unattended worker find what a person would retire (27.4b 改, 段 D).

    Reads a rate only where there is something to read it from. With no markers
    yet, "0% recall" and "no evidence" print as the same number and mean
    opposite things, so this says which one it is.
    """
    with transaction(args.dsn) as cur:
        got = metrics.retirement_eval(cur, sample=args.sample)

    print("markers (what a person said was finished, before the night's extraction ran)")
    print(f"  {got['markers']} marker(s) in all")
    if got["unattributed"]:
        print(f"    {got['unattributed']} fell outside any finished session")
    if got["unread"]:
        print(f"    {got['unread']} are in sessions the extractor has not read yet")
    if got["recall"] is None:
        print("  recall: no evidence yet — no marker sits in a session that was extracted")
    else:
        print(
            f"  recall: {got['recall']:.0%}  ({got['recovered']} recovered, {got['missed']} missed)"
        )
        for title in got["missed_titles"]:
            print(_wrap(title, indent="    missed: "))

    print("\nthe worker's own retirement proposals")
    print(f"  {got['proposed']} filed, {got['decided']} decided, {got['pending']} still waiting")
    if got["precision"] is None:
        print("  precision: no evidence yet — none of them has been decided")
    else:
        print(f"  precision: {got['precision']:.0%}  ({got['approved']} of {got['decided']} kept)")

    if got["sample"]:
        print(f"\nstill true? a sample of {len(got['sample'])} adopted memories to read")
        print(
            _wrap(
                "Recall above only finds retirements somebody noticed. What it cannot "
                "see is the ones nobody said out loud, and those go on being handed "
                "over as current with nothing in the queue to mention them.",
                indent="  ",
            )
        )
        print()
        for row in got["sample"]:
            print(f"  {_short(row['memory_id'])}  [{row['scope_name']}]  {row['type']}")
            print(_wrap(row["title"], indent="      "))
    return 0


def cmd_trial(args) -> int:
    """Open or close a switchover window, or read what happened inside it (27.5).

    The switch itself is a setting on another program, so this system cannot
    perform it and cannot verify it. What it can do is mark when the window
    opened, which is what turns the accident count afterwards into a count of
    something. Without a mark, a tally is over whatever has accumulated.
    """
    with transaction(args.dsn) as cur:
        if args.open:
            opened = incidents.open_trial(cur, note=args.note or "", actor=args.actor)
            print(f"window open from {opened['created_at']:%Y-%m-%d %H:%M}")
            print(_wrap(args.note, indent="  "))
            return 0
        if args.close:
            open_now = incidents.current_trial(cur)
            incidents.close_trial(cur, note=args.note or "", actor=args.actor)
            days = _days_since(open_now["opened_at"])
            print(f"window closed after {days} day(s)")
            return 0

        open_now = incidents.current_trial(cur)
        if not open_now:
            print("no window is open.  'mashu trial --open --note <what is switched off>'")
            return 0
        since = open_now["opened_at"]
        print(f"window open since {since:%Y-%m-%d %H:%M}  ({_days_since(since)} day(s))")
        note = (open_now["detail"] or {}).get("note")
        if note:
            print(_wrap(note, indent="  "))

        counts = incidents.tally(cur, since=since)
        if not counts:
            print("\n  no accidents inside the window")
            return 0
        print()
        for row in counts:
            cause = IncidentCause(row["cause"])
            print(f"  {row['kind']:<8}{row['cause']:<11}{row['n']:>3}   -> {LEADS_TO[cause]}")
    return 0


def _days_since(when) -> int:
    from datetime import datetime

    return (datetime.now(tz=when.tzinfo) - when).days


def cmd_incident(args) -> int:
    """Record an accident, or read the tally back (27.5, 30 段 D).

    27.5 will not have the switchover judged by feel, so what is kept is
    occurrences and the cause of each. The cause carries the value: each one
    sends you somewhere different, and a count without it says only that
    something went wrong.

    Nothing infers an accident. Deciding one happened means knowing what the
    session should have known, which is not a thing the system can see from the
    inside, so this is written by hand and the only help it gives is refusing a
    record with no account of what happened.
    """
    with transaction(args.dsn) as cur:
        if not args.cause:
            rows = incidents.listed(cur, since_days=args.days)
            counts = incidents.tally(cur, since_days=args.days)
            if not rows:
                window = f" in the last {args.days} day(s)" if args.days else ""
                print(f"no accidents recorded{window}")
                return 0
            for row in counts:
                cause = IncidentCause(row["cause"])
                print(f"{row['kind']:<8}{row['cause']:<11}{row['n']:>3}   -> {LEADS_TO[cause]}")
            print()
            for row in rows:
                named = f"  [{row['scope_name']}]" if row["scope_name"] else ""
                print(
                    f"  {row['occurred_at']:%Y-%m-%d}  {row['kind']}/{row['cause']}{named}"
                    f"{'  ' + _short(row['memory_id']) if row['memory_id'] else ''}"
                )
                print(_wrap(row["note"], indent="      "))
                if row["title"]:
                    print(_wrap(row["title"], indent="      | "))
                print()
            return 0

        cause = IncidentCause(args.cause)
        kind = next(k for k, causes in incidents.CAUSES.items() if cause in causes)
        entity = _resolve_entity(cur, args.memory) if args.memory else None
        made = incidents.record(
            cur,
            kind=kind,
            cause=cause,
            note=args.note,
            recorded_by=args.actor,
            memory_id=entity["memory_id"] if entity else None,
            scope_id=_scope_by_name(cur, args.scope) if args.scope else None,
            occurred_at=args.on,
        )
        print(f"{made['kind']}/{made['cause']}  {_short(made['incident_id'])}")
        print(_wrap(f"leads to: {LEADS_TO[cause]}", indent="  "))
    return 0


def cmd_stale(args) -> int:
    """Adopted rules that name a moment in themselves (25.2 移行, 30 段 C).

    A window written down in the years before there was anywhere to put one.
    Section 25.2 gives two ways out and does not choose between them: move it to
    a temporary context that expires by the clock, or rewrite it without the
    date so what is left is indefinite and true. Which one applies is a reading
    of the content, so nothing here decides.

    A screen, not a verdict, and the match is printed so waving off a wrong one
    costs a glance. Some of these are rules *about* shelf life rather than rules
    *with* one, and no pattern tells those apart.
    """
    with transaction(args.dsn) as cur:
        found = metrics.self_dating(cur)
        if not found:
            print("nothing adopted names a moment in itself")
            return 0
        print(f"{len(found)} adopted rule(s) name a moment in themselves\n")
        for row in found:
            pushed = "" if row["delivery"] == str(Delivery.PULL_ONLY) else f"  [{row['delivery']}]"
            print(
                f"  {_short(row['memory_id'])}  {row['matched']!r} in {row['matched_in']}"
                f"  [{row['scope_name']}]{pushed}"
            )
            print(_wrap(row["title"], indent="      "))
            print()
        print(
            _wrap(
                "each is either a window — 'mashu retire <id> dormant --reason', then "
                "'mashu remember --until <when> --kind fact|preference' — or a rule that "
                "reads as dated and is not, in which case rewrite it without the date. "
                "The screen does not tell them apart.",
                indent="  ",
            )
        )
    return 0


def _span(seconds: float | None) -> str:
    """A duration at the coarsest unit that still says something."""
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def cmd_directive(args) -> int:
    """Write the short standing form a push carries (21.2). The user's call only.

    Without this the short form could only be written when the memory was
    created, so everything already in the store was pushable only at full
    length. Against a 2,000 token ceiling that meant a session opening could
    carry two or three rules out of dozens, and the way to add one to an
    existing memory was to file a revision whose body had not changed.
    """
    if not args.clear and not args.text:
        raise SystemExit("give the short form, or --clear to take it off")
    try:
        with transaction(args.dsn) as cur:
            entity = _resolve_entity(cur, args.memory)
            changed = store.set_directive(
                cur,
                memory_id=entity["memory_id"],
                directive=None if args.clear else args.text,
                actor=args.actor,
            )
    except DeliveryError as refusal:
        print(f"not changed: {refusal}", file=sys.stderr)
        return 1
    print(f"{changed['title']}")
    if changed["directive"] is None:
        print("  > (no directive; a push would carry the whole content)")
    else:
        print(_wrap(changed["directive"], indent="  > "))
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
# capture
# --------------------------------------------------------------------------
def cmd_work(args) -> int:
    """Work through the extraction queue (16.3).

    This is the process that makes the store fill itself. Everything it does is
    reversible by a reviewer and nothing it does removes knowledge on its own,
    which is what makes it safe to leave running.
    """
    try:
        extractor = extract.get_extractor(args.extractor)
    except extract.ExtractionError as failure:
        print(f"no extractor: {failure}", file=sys.stderr)
        return 1

    print(f"extractor  {extractor.name} / {extractor.model}")
    outcomes = worker.run_once(
        args.dsn, extractor=extractor, limit=args.limit, dry_run=args.dry_run
    )
    if not outcomes:
        print("nothing queued")
        return 0
    for outcome in outcomes:
        print(outcome.line())
        for refusal in outcome.refused:
            print(f"    refused: {refusal}")
    return 0


def cmd_sweep(args) -> int:
    """Enqueue transcripts no hook ever claimed (16.3).

    A hook that did not fire leaves nothing behind saying so, so the only way
    to find out is to compare what is on disk with what the ledger has seen.
    """
    found = worker.sweep(args.dsn, since_days=args.days, limit=args.limit)
    if not found:
        print("nothing new on disk")
        return 0
    print(f"enqueued {len(found)} transcript(s)")
    for run in found:
        print(f"  {_short(run['run_id'])}  {run['source_cli']}  {run['cwd'] or '-'}")
    return 0


def cmd_route(args) -> int:
    """The map from a working directory to a scope (16.3).

    Stated, never inferred. A memory filed under the wrong scope is not a worse
    version of a right one; it sits where the sessions that need it will never
    look, and nothing about it reads as wrong.
    """
    with transaction(args.dsn) as cur:
        if args.add:
            scope_id = _scope_by_name(cur, args.scope)
            row = routing.add(cur, path_prefix=args.add, scope_id=scope_id, created_by=args.actor)
            released = runs.release(cur, cwd_prefix=row["path_prefix"])
            print(f"{row['path_prefix']}  ->  {args.scope}")
            if released:
                print(f"released {released} held transcript(s) back into the queue")
            return 0
        if args.ignore:
            row = routing.add(cur, path_prefix=args.ignore, scope_id=None, created_by=args.actor)
            released = runs.release(cur, cwd_prefix=row["path_prefix"])
            print(f"{row['path_prefix']}  ->  (no scope, on purpose)")
            if released:
                print(f"released {released} held transcript(s) back into the queue")
            return 0
        if args.remove:
            print("removed" if routing.remove(cur, path_prefix=args.remove) else "no such route")
            return 0

        rows = routing.all_routes(cur)
        if not rows:
            print("no routes; every transcript will be held until one exists")
            return 0
        for row in rows:
            print(f"{row['path_prefix']}\n    -> {row['scope_name'] or '(no scope, on purpose)'}")
    return 0


# --------------------------------------------------------------------------
# retirement
# --------------------------------------------------------------------------
def cmd_retire(args) -> int:
    """Retire what a person says is finished or wrong (16.1, 30 段 B).

    Section 16.1 has always named the user's own statement as a trigger for
    retirement, and there was no way for a person to make one: retiring
    anything meant waiting for an agent to propose it. This is that entrance.

    disproven is Human Review Required under section 17 whoever asks for it,
    and the person typing this command with a reason is that review. It is
    recorded as a review rather than waved past one, so the log says who
    decided and on what grounds.
    """
    target = VersionStatus(args.status)
    with transaction(args.dsn) as cur:
        entity = _resolve_entity(cur, args.memory_id)
        full = store.get_entity(cur, entity["memory_id"])
        version_id = full["active_version"]
        if version_id is None:
            print(f"{full['title']} has no active version to retire", file=sys.stderr)
            return 1

        try:
            made = proposals.propose(
                cur,
                actor=args.actor,
                operation=ProposalOperation.CHANGE_STATUS,
                payload={
                    "version_id": str(version_id),
                    "status": str(target),
                    "reason": args.reason,
                    "source_type": str(SourceType.USER),
                },
                target_memory=full["memory_id"],
            )
        except DuplicateProposalError as clash:
            # This is the intended path, not an edge case. The worker proposes
            # a retirement, search shows it riding along with the content, the
            # person agrees and types this — and what agreeing means is
            # approving the proposal that is already standing, not filing a
            # second one beside it.
            return _agree(cur, args, full, clash)
        proposal = made["proposal"]
        if proposal["status"] == str(ProposalStatus.PENDING):
            proposals.approve(
                cur,
                proposal["proposal_id"],
                reviewer=args.reviewer,
                reason=f"stated at the terminal: {args.reason}",
            )
            print(f"{full['title']}  ->  {target}  (reviewed here: {made['ruling'].reason})")
        else:
            print(f"{full['title']}  ->  {target}")
    return 0


# --------------------------------------------------------------------------
# review, in one sitting
# --------------------------------------------------------------------------
def _agree(cur, args, entity, clash: DuplicateProposalError) -> int:
    """Approve the standing proposal this command was agreeing with."""
    waiting = [row for row in clash.existing if row["status"] == "pending"]
    if not waiting:
        why = clash.declined[-1]["decision_reason"] if clash.declined else "already decided"
        print(f"{entity['title']}: this was already ruled on ({why})", file=sys.stderr)
        return 1

    proposal = waiting[-1]
    proposals.approve(
        cur,
        proposal["proposal_id"],
        reviewer=args.reviewer,
        reason=f"agreed at the terminal: {args.reason}",
    )
    print(
        f"{entity['title']}  ->  {proposal['payload'].get('status')}  "
        f"(approved the proposal {proposal['actor']} was already holding)"
    )
    return 0


def cmd_review(args) -> int:
    """Work through what is waiting, at the pace it is read (18.1, 30 段 C).

    Section 18.1 settled the unit: a session bundle, its context built once,
    passed wholesale with the exceptions taken out by hand. What it did not
    settle is the motion, and the motion is where the cost turned out to sit.
    Printing a bundle whole and then asking for one line of decisions makes the
    reader hold twenty items and their numbers in mind while composing a
    command about text that has already scrolled off. The unit was right and
    the handling was wrong, and a review that is optional does not survive
    handling that is unpleasant.

    So a bundle arrives as a list of its titles that the arrow keys move
    through, one keystroke decides an item, and the reader who wants to pass
    the whole bundle unread — which 18.1 expects to be the common case — presses
    one key for that too. Every decision commits as it is made, so stopping in
    the middle keeps everything already decided.
    """
    with transaction(args.dsn) as cur:
        waiting = proposals.session_queue(cur)
        bundles = _wanted(cur, waiting, args)

    if not bundles:
        if waiting:
            print(f"nothing new; {len(waiting)} bundle(s) are put off. --all to see them")
        else:
            print("nothing waiting for review")
        return 0

    if args.batch is not None or not sys.stdin.isatty():
        return _one_shot(args, bundles[0])
    return _sitting(args)


def _wanted(cur, waiting: list[dict], args) -> list[dict]:
    """The bundles this run is about: one named, or every one with something new.

    --all has to widen the bundles as well as the items inside them. A bundle
    whose every item was put off has deferred == count, so the ordinary filter
    drops it, and a reader following the advice to pass --all would have been
    shown the same nothing again.
    """
    if args.bundle:
        wanted = None if args.bundle == "none" else _resolve_session(cur, args.bundle)
        return [b for b in waiting if b["session_id"] == wanted]
    if args.all:
        return list(waiting)
    return [b for b in waiting if b["deferred"] < b["count"]]


def _undecided(bundle: dict, args) -> list[dict]:
    """A bundle's items, minus what was already put off unless --all asks for it."""
    return [i for i in bundle["proposals"] if args.all or not i["deferred_at"]]


def _one_shot(args, bundle: dict) -> int:
    """The bundle printed whole and decided by one script, for pipes and tests."""
    with transaction(args.dsn) as cur:
        items = _undecided(bundle, args)
        _print_review(cur, bundle, items)
    if args.batch is None:
        print("\nnot a terminal; pass --batch to decide non-interactively")
        return 0
    return _apply_review(args, items, args.batch)


# --------------------------------------------------------------------------
# reading one key at a time
# --------------------------------------------------------------------------
_TOKENS = {
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\r": "enter",
    "\n": "enter",
    "\x7f": "left",
    "\x1b": "left",
    "\x03": "q",
    "\x04": "q",
    " ": "space",
}


def _getkey() -> str:
    """One keystroke, without waiting for a return.

    A review that costs a whole typed line per decision is a review that does
    not happen. Terminals hand arrow keys over as escape sequences, so the
    escape has to be read and then looked at again: on its own it means go
    back, and followed by a bracket it is an arrow.

    Where there is no terminal to put into this mode, a typed line stands in
    for a keystroke, and everything above still works — one key more.
    """
    if termios is None or not sys.stdin.isatty():
        try:
            typed = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return "q"
        return _TOKENS.get(typed, typed[:1].lower() or "enter")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        key = _byte(fd)
        # Read the descriptor rather than sys.stdin. A text stream keeps its own
        # buffer, so the bracket and the letter of an arrow sequence can already
        # be inside Python while select still reports the descriptor as empty,
        # and every arrow key then arrives as three unrelated keystrokes.
        if key == "\x1b" and select.select([fd], [], [], 0.05)[0]:
            key += _byte(fd)
            if key.endswith("[") and select.select([fd], [], [], 0.05)[0]:
                key += _byte(fd)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return _TOKENS.get(key, key.lower())


def _byte(fd: int) -> str:
    """One byte off the terminal, with the end of input read as leaving."""
    try:
        raw = os.read(fd, 1)
    except OSError:
        return "\x04"
    return raw.decode("utf-8", "replace") if raw else "\x04"


def _typed(prompt: str) -> str | None:
    """A line, for the reasons 18.1 makes mandatory. Nothing typed cancels the decision."""
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer:
        print("  never mind")
        return None
    return answer


def _screen(text: str) -> None:
    """Repaint. The page the reader is deciding about should be the whole view."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[H\x1b[2J")
    print(text)


# --------------------------------------------------------------------------
# a sitting
# --------------------------------------------------------------------------
_SHELF_KEYS = "  ↑↓ move   ⏎ open   a approve it all unread   s put it off   q leave"
_LIST_KEYS = (
    "  ↑↓ move   ⏎ open   a approve the rest   s put the bundle off\n  ← the bundles   q leave"
)
_ITEM_KEYS = (
    "  y approve   r turn down   e edit   s put off   a approve the rest\n"
    "  space read on   b top   ↑↓ another item   ← the list   q leave"
)

_WORD = {"a": "approved", "r": "declined", "s": "put off", "e": "edited"}
_MARK = {"approved": "✓", "declined": "✗", "put off": "·", "edited": "✎"}


def _sitting(args) -> int:
    """The bundles waiting, chosen from a list and worked one at a time.

    Which bundle comes first is an accident of when its session ended, so a
    sitting that opens on the oldest can open on the largest, and the reader
    who has ten minutes has no way to spend them on something smaller. The
    list is the way out: everything waiting, its size and its age beside it,
    and any of it opened in one keystroke.

    With one bundle waiting there is nothing to choose between, so that one
    opens straight away.
    """
    shelf = _shelf(args)
    at, note = 0, ""
    reading = len(shelf) == 1

    while True:
        if not shelf:
            if note:
                print(note)
            print("\nnothing else waiting")
            return 0
        at = min(at, len(shelf) - 1)

        if not reading:
            _screen(_bundles(shelf, at, note))
            key = _getkey()
            if key == "q":
                return 0
            if key in ("down", "j"):
                at = min(len(shelf) - 1, at + 1)
                continue
            if key in ("up", "k"):
                at = max(0, at - 1)
                continue
            if key == "a":
                note = _whole(args, shelf[at])
                shelf = _shelf(args)
                continue
            if key == "s":
                reason = _typed("  why put the whole bundle off? ")
                if reason is None:
                    continue
                note = _whole(args, shelf[at], verb="s", reason=reason)
                shelf = _shelf(args)
                continue
            if key not in ("enter", "right", "l"):
                continue

        reading = False
        here = shelf[at]["session_id"]
        outcome, note = _open(args, shelf[at], at + 1, len(shelf))
        if outcome == "leave":
            print(note)
            print("\nleft. what was decided is kept; 'mashu review' opens on the rest")
            return 0

        shelf = _shelf(args)
        if outcome == "next":
            # Move past the bundle just worked, unless finishing it took the
            # bundle off the list, in which case this place already holds the
            # next one.
            where = next((i for i, b in enumerate(shelf) if b["session_id"] == here), None)
            at = where + 1 if where is not None else at


def _shelf(args) -> list[dict]:
    """Everything still waiting, as it stands now rather than as it stood at the start."""
    with transaction(args.dsn) as cur:
        waiting = proposals.session_queue(cur)
        return [b for b in _wanted(cur, waiting, args) if _undecided(b, args)]


def _bundles(shelf: list[dict], at: int, note: str) -> str:
    """Everything waiting, so a reader with ten minutes can spend them on ten minutes."""
    total = sum(len(_pending(b)) for b in shelf)
    width = _width()
    rows = []
    for number, bundle in enumerate(shelf):
        items = _pending(bundle)
        scope = next((i["scope_name"] for i in items if i["scope_name"]), "-")
        first = items[0]["title"] if items else ""
        row = _clip(
            f" {number + 1:>3}  {_pad(scope, 14)} {len(items):>3} to decide  "
            f"{bundle['days_pending']:>2} day(s)   {first or ''}",
            width - 1,
        )
        rows.append(f"\x1b[1m▸{row[1:]}\x1b[0m" if number == at else row)

    shown, above, below = _shown(rows, at, reserve=7 + (2 if note else 0))
    lines = [f"{len(shelf)} bundle(s) waiting, {total} to decide\n"]
    lines += [line for line in (above, *shown, below) if line]
    if note:
        lines.append(note)
    lines.append("\n" + _SHELF_KEYS)
    return "\n".join(lines)


def _pending(bundle: dict) -> list[dict]:
    """A bundle's items as the list screen counts them: what is not put off."""
    return [i for i in bundle["proposals"] if not i["deferred_at"]] or bundle["proposals"]


def _open(args, summary, place: int, total: int) -> tuple[str, str]:
    """Read one bundle in, fresh, and hand it to the reader."""
    with transaction(args.dsn) as cur:
        bundle = next(
            (b for b in proposals.session_queue(cur) if b["session_id"] == summary["session_id"]),
            None,
        )
        items = _undecided(bundle, args) if bundle else []
        if not items:
            return "next", ""
        near = _look_alikes(cur, items)
        pages = [
            _item_text(cur, item, n, len(items), near.get(item["target_memory"], []))
            for n, item in enumerate(items, 1)
        ]
    return _bundle_sitting(args, bundle, items, pages, place, total)


def _look_alikes(cur, items) -> dict:
    """The bundle's look-alikes, asked for once per scope rather than per item."""
    wanted = [item["target_memory"] for item in items if item["target_memory"]]
    if not wanted:
        return {}
    cur.execute(
        "SELECT memory_id, scope_id FROM memory_entity WHERE memory_id = ANY(%s)", (wanted,)
    )
    by_scope: dict = {}
    for row in cur.fetchall():
        by_scope.setdefault(row["scope_id"], []).append(row["memory_id"])
    found: dict = {}
    for scope_id, memory_ids in by_scope.items():
        found.update(resolution.look_alikes(cur, scope_id=scope_id, memory_ids=memory_ids))
    return found


def _whole(args, summary, verb: str = "a", reason: str = "") -> str:
    """A bundle decided from the list, without opening it."""
    with transaction(args.dsn) as cur:
        bundle = next(
            (b for b in proposals.session_queue(cur) if b["session_id"] == summary["session_id"]),
            None,
        )
        items = _undecided(bundle, args) if bundle else []
    if not items:
        return ""
    done: dict[int, str] = {}
    refused = _rest(args, items, done, verb=verb, reason=reason)
    return _tally(bundle, done, len(items)) + _said(refused)


def _bundle_sitting(args, bundle, items, pages, place, total) -> tuple[str, str]:
    """One bundle, from its list of titles down to its last item.

    Returns where to go next — leave, the next bundle, or back up to the list of
    bundles — and what this one came to, which the caller shows on a screen that
    survives the repaint.
    """
    done: dict[int, str] = {}
    at = 0
    reading = False
    scroll = 0
    more = 0
    note = ""

    while True:
        if reading:
            page, more = _paged(pages[at], scroll, said=note)
            _screen(page + _standing(done, at) + _said(note) + "\n" + _ITEM_KEYS)
        else:
            _screen(_contents(bundle, items, place, total, done, at) + _said(note))
        key = _getkey()
        if key not in ("y", "enter", "r", "n", "s", "e", "a"):
            note = ""

        if key == "space":
            # Read on where a proposal is longer than the screen, and step to
            # the next one where it is not: the same key, and in both cases it
            # means carry on.
            if reading and more:
                scroll = more
                continue
            key = "down"
        if key == "b" and reading:
            scroll = 0
            continue

        if key == "q":
            return "leave", _tally(bundle, done, len(items))
        if key in ("down", "j"):
            at = min(len(items) - 1, at + 1)
            scroll = 0
            continue
        if key in ("up", "k"):
            at = max(0, at - 1)
            scroll = 0
            continue
        if key == "a":
            note = _rest(args, items, done)
            if note:
                continue
            break
        if not reading:
            if key in ("enter", "right", "l"):
                reading = True
            elif key == "left":
                return "up", _tally(bundle, done, len(items))
            elif key == "s":
                reason = _typed("  why put the whole bundle off? ")
                if reason is None:
                    continue
                note = _rest(args, items, done, verb="s", reason=reason)
                if note:
                    continue
                break
            continue

        if key in ("left", "l"):
            reading = False
            continue
        if key in ("y", "enter"):
            note = _settle(args, items[at], done, at, "a")
        elif key in ("r", "n"):
            reason = _typed("  why turn it down? ")
            if reason is None:
                continue
            note = _settle(args, items[at], done, at, "r", reason)
        elif key == "s":
            reason = _typed("  why put it off? ")
            if reason is None:
                continue
            note = _settle(args, items[at], done, at, "s", reason)
        elif key == "e":
            note = _settle(args, items[at], done, at, "e")
        else:
            continue

        # A decision the store refused is not a decision, so the reader stays
        # where they are and reads why rather than finding themselves one item
        # further on with nothing recorded behind them.
        if note:
            continue
        if at + 1 >= len(items):
            break
        at += 1
        scroll = 0

    return "next", _tally(bundle, done, len(items))


def _rest(args, items, done, verb: str = "a", reason: str = "") -> str:
    """Everything in the bundle not decided yet, decided the same way.

    One item the store refuses does not stop the rest of them: what it refused
    is collected and said once, afterwards.
    """
    refused = []
    for number, item in enumerate(items):
        if number in done:
            continue
        note = _settle(args, item, done, number, verb, reason)
        if note:
            refused.append(note)
    return "\n".join(refused)


def _settle(args, item, done, number: int, verb: str, reason: str = "") -> str:
    """Carry one decision into the store, in a transaction of its own.

    One transaction per item is what lets a reader stop anywhere: what is
    behind them is committed, and a sitting does not have to be finished to
    have been worth starting. That only holds if a refusal stops the decision
    and not the sitting, so what the store declines to do comes back as
    something to say and the reader keeps their place. Returns that, empty
    when it simply worked.
    """
    if number in done:
        return (
            f"  already {done[number]}. a decision is a record and does not get taken back;\n"
            "  to put this content back, write it as your own with 'mashu remember'"
        )
    edited = _edit_text(args, item) if verb == "e" else None
    try:
        with transaction(args.dsn) as cur:
            _decide(cur, args, item, verb, reason, edited=edited)
    except MashuError as refusal:
        return f"  {refusal}"
    done[number] = _WORD[verb]
    return ""


def _said(note: str) -> str:
    """A refusal, under the screen it belongs to rather than in place of it."""
    return f"\n{note}" if note else ""


def _standing(done: dict, at: int) -> str:
    """Whether this item was already decided in this sitting, said on the page itself."""
    return f"\n  ({done[at]} in this sitting)" if at in done else ""


def _width() -> int:
    """How wide the terminal is, not how wide the writing was laid out to be."""
    return max(40, shutil.get_terminal_size((WIDTH, 24)).columns)


def _room(reserve: int) -> int:
    """How many lines are left for a list or a page once the fixed parts have theirs.

    A screen that overruns the terminal is a screen the reader has to scroll
    back through to see its own heading, and on a short console that is every
    screen. So nothing is printed that does not fit: what will not fit is said
    as a count instead.
    """
    return max(4, shutil.get_terminal_size((WIDTH, 24)).lines - reserve)


def _rows(text: str, width: int) -> int:
    """How many terminal lines one written line takes once it wraps."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return max(1, -(-_cells(plain) // width))


def _shown(rows: list[str], at: int, reserve: int) -> tuple[list[str], str, str]:
    """The part of a list that fits, kept around wherever the cursor is."""
    room = _room(reserve)
    if len(rows) <= room:
        return rows, "", ""
    room -= 2  # the two lines that say what is not being shown
    top = max(0, min(at - room // 2, len(rows) - room))
    above = f"  ↑ {top} more" if top else ""
    below = f"  ↓ {len(rows) - top - room} more" if top + room < len(rows) else ""
    return rows[top : top + room], above, below


def _paged(text: str, offset: int, said: str = "") -> tuple[str, int]:
    """As much of one proposal as fits, and where the next screenful would start.

    The first lines are held on screen whatever the offset: what is being
    decided about should not scroll away from the deciding.
    """
    width = _width()
    lines = text.splitlines()
    head, body = lines[:3], lines[3:]
    # what the page costs besides its body: the heading, the line that says
    # whether it was already decided, a blank, and the two lines of keys
    room = _room(
        sum(_rows(line, width) for line in head)
        + 5
        + sum(_rows(line, width) for line in said.splitlines())
    )
    shown, used = [], 0
    for line in body[offset:]:
        cost = _rows(line, width)
        if used + cost > room:
            break
        shown.append(line)
        used += cost
    left = len(body) - offset - len(shown)
    if left <= 0:
        return "\n".join(head + shown), 0
    return "\n".join(head + shown + [f"  … {left} more line(s), space to go on"]), (
        offset + len(shown)
    )


def _cells(text: str) -> int:
    """How wide this is on a terminal, counting the double-width characters as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _clip(text: str, cells: int) -> str:
    """Cut a title to fit, measuring in cells rather than in characters."""
    if _cells(text) <= cells:
        return text
    out, used = [], 0
    for ch in text:
        used += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used > cells - 1:
            break
        out.append(ch)
    return "".join(out) + "…"


def _pad(text: str, cells: int) -> str:
    """Fill a column out to a width, counting cells so a Japanese name lines up too."""
    clipped = _clip(text, cells)
    return clipped + " " * max(0, cells - _cells(clipped))


def _contents(bundle, items, place: int, total: int, done: dict, at: int) -> str:
    """The bundle as a list of its titles: what it takes to pass it unread."""
    name = _short(bundle["session_id"]) if bundle["session_id"] else "none"
    scope = (items[0]["scope_name"] or "-") if items else "-"
    lines = [
        f"{scope}  bundle {name}  {len(items)} to decide, "
        f"waiting {bundle['days_pending']} day(s)    "
        f"bundle {place} of {total}\n"
    ]
    width = _width()
    rows = []
    for number, item in enumerate(items):
        mark = _MARK.get(done.get(number), " ")
        title = item["title"] or _short(item["proposal_id"])
        row = _clip(
            f" {mark} {number + 1:>3}  {item['operation']:<15} "
            f"{item['memory_type'] or '-':<14} {title}",
            width - 1,
        )
        rows.append(f"\x1b[1m▸{row[1:]}\x1b[0m" if number == at else f" {row[1:]}")

    shown, above, below = _shown(rows, at, reserve=8)
    lines += [line for line in (above, *shown, below) if line]
    lines.append("\n" + _LIST_KEYS)
    return "\n".join(lines)


def _item_text(cur, item, number: int, of: int, near: list[dict] = ()) -> str:
    """One proposal as a page of its own, beside what it would displace (18.1)."""
    label = f" {number} of {of} "
    across = _across()
    lines = [
        "─" * 4 + label + "─" * max(4, across - 4 - _cells(label)),
        f"{item['operation']}  {item['memory_type'] or '-'}  "
        f"[{item['scope_name'] or '-'}]  {_short(item['proposal_id'])}",
        f"{item['title'] or ''}",
        "",
    ]
    if item["review_note"]:
        lines.append(_wrap(f"(put off earlier: {item['review_note']})"))
        lines.append("")
    lines.append(_diff_text(cur, item))
    resembling = _look_alike_text(list(near))
    if resembling:
        lines.append(resembling)
    lines.append("─" * across)
    return "\n".join(lines)


def _tally(bundle, done: dict, count: int) -> str:
    """What the bundle came to, said once, so the next one starts on a clean line."""
    name = _short(bundle["session_id"]) if bundle["session_id"] else "none"
    parts = []
    for word in ("approved", "declined", "edited", "put off"):
        many = sum(1 for value in done.values() if value == word)
        if many:
            parts.append(f"{many} {word}")
    left = count - len(done)
    if left:
        parts.append(f"{left} left undecided")
    return f"\nbundle {name}: " + ", ".join(parts or ["nothing decided"])


# --------------------------------------------------------------------------
# the whole bundle at once
# --------------------------------------------------------------------------
_REVIEW_HELP = """
  all                 approve everything still undecided here
  r N reason          turn item N down, with the reason
  e N                 edit item N's text, then approve it as your own
  s N reason          put item N off, saying why
  q                   leave (anything undecided stays undecided)

  several may be given at once, separated by ';'
"""


def _print_review(cur, bundle, items) -> None:
    """The bundle as one reading, each proposal against what it would replace."""
    name = _short(bundle["session_id"]) if bundle["session_id"] else "none"
    scope = items[0]["scope_name"] if items else "-"
    print(
        f"bundle {name}  [{scope or '-'}]  {len(items)} to decide, "
        f"waiting {bundle['days_pending']} day(s)\n"
    )
    for index, item in enumerate(items, 1):
        print("-" * WIDTH)
        print(
            f"{index:>3}  {_short(item['proposal_id'])}  {item['operation']}  "
            f"{item['memory_type'] or '-'}"
        )
        print(f"     {item['title'] or ''}\n")
        if item["review_note"]:
            print(_wrap(f"(put off earlier: {item['review_note']})"))
            print()
        _print_diff(cur, item)
    print("-" * WIDTH)


def _print_diff(cur, item) -> None:
    print(_diff_text(cur, item))


def _diff_text(cur, item) -> str:
    """What this proposal says, beside what it would displace (18.1)."""
    cur.execute(
        "SELECT applied_version FROM proposal WHERE proposal_id = %s", (item["proposal_id"],)
    )
    version_id = (cur.fetchone() or {}).get("applied_version")
    if version_id is None:
        lines = [_wrap("(nothing written yet; this proposal changes nothing until approved)")]
        for key, value in (item["payload"] or {}).items():
            if key in ("reason", "status"):
                lines.append(_wrap(f"{key}: {value}"))
        return "\n".join(lines)

    version = store.get_version(cur, version_id)
    entity = store.get_entity(cur, version["memory_id"])
    lines = []
    if version["directive"]:
        lines.append(_wrap(version["directive"], indent="  > "))
        lines.append("")
    lines.append(_wrap(version["content"]))
    grounds = _grounds_text(cur, version_id)
    if grounds:
        lines.append(grounds)

    current = entity["active_version"]
    if current and current != version_id:
        lines.append("\n     replacing what is active now:")
        lines.append(_wrap(store.get_version(cur, current)["content"], indent="   | "))
    elif current is None:
        lines.append("\n     (new; nothing is active on this entity yet)")
    return "\n".join(lines)


def _apply_review(args, items, script: str) -> int:
    """Carry the sitting's decisions into the store, one transaction each."""
    if not script or script.lower() in ("q", "quit"):
        print("left undecided")
        return 0

    decided: dict[int, tuple[str, str]] = {}
    approve_rest = False
    for clause in script.split(";"):
        clause = clause.strip()
        if not clause:
            continue
        if clause.lower() in ("all", "a"):
            approve_rest = True
            continue
        head, _, rest = clause.partition(" ")
        verb = head.lower()
        if verb not in ("r", "e", "s"):
            print(f"do not know what to do with {clause!r}", file=sys.stderr)
            return 1
        number, _, reason = rest.strip().partition(" ")
        if not number.isdigit() or not 1 <= int(number) <= len(items):
            print(f"{number!r} is not one of 1..{len(items)}", file=sys.stderr)
            return 1
        if verb in ("r", "s") and not reason.strip():
            print(f"'{verb} {number}' needs a reason", file=sys.stderr)
            return 1
        decided[int(number)] = (verb, reason.strip())

    for index, item in enumerate(items, 1):
        verb, reason = decided.get(index, ("a" if approve_rest else "", ""))
        if not verb:
            continue
        # The editor runs before the transaction opens. Waiting on a human
        # inside one holds the entity's locks for as long as they take to
        # think, and a managed database will end the transaction before they
        # are done.
        edited = _edit_text(args, item) if verb == "e" else None
        with transaction(args.dsn) as cur:
            _decide(cur, args, item, verb, reason, edited=edited)
    return 0


def _decide(cur, args, item, verb: str, reason: str, edited: str | None = None) -> None:
    proposal_id = item["proposal_id"]
    title = item["title"] or _short(proposal_id)

    if verb == "r":
        proposals.reject(cur, proposal_id, reviewer=args.reviewer, reason=reason)
        print(f"declined  {title}")
        return
    if verb == "s":
        proposals.defer(cur, proposal_id, reviewer=args.reviewer, note=reason)
        print(f"put off   {title}  ({reason})")
        return
    if verb == "e":
        _edit(cur, args, item, edited)
        return
    proposals.approve(cur, proposal_id, reviewer=args.reviewer, reason=args.reason)
    print(f"approved  {title}")


def _edit_text(args, item) -> str | None:
    """Open the reviewer's editor on what this proposal wrote, outside any transaction."""
    with transaction(args.dsn) as cur:
        cur.execute(
            "SELECT applied_version FROM proposal WHERE proposal_id = %s", (item["proposal_id"],)
        )
        version_id = (cur.fetchone() or {}).get("applied_version")
        if version_id is None:
            return None
        original = store.get_version(cur, version_id)["content"]
    return _open_editor(original)


def _edit(cur, args, item, edited: str | None) -> None:
    """Approve, then record the reviewer's own wording as a version of theirs (18.1).

    Not an overwrite. Versions are immutable, so what an edit produces is a new
    version whose source is the user, sitting on top of what the agent proposed.
    The history then says both things: what was suggested, and what a person
    made of it.

    It goes through a proposal like everything else. Section 15 makes the
    proposal the record of why the store looks as it does, and a version
    written straight into the store is a change with no such record — the one
    kind of gap the audit trail cannot show as a gap.
    """
    cur.execute(
        "SELECT applied_version FROM proposal WHERE proposal_id = %s", (item["proposal_id"],)
    )
    version_id = (cur.fetchone() or {}).get("applied_version")
    if version_id is None:
        print(f"nothing written yet to edit on {item['title']}", file=sys.stderr)
        return

    original = store.get_version(cur, version_id)["content"]
    proposals.approve(cur, item["proposal_id"], reviewer=args.reviewer, reason="edited on review")
    if edited is None or edited.strip() == original.strip():
        print(f"approved  {item['title']}  (unchanged)")
        return

    entity = store.get_entity(cur, store.get_version(cur, version_id)["memory_id"])
    proposals.propose(
        cur,
        actor=args.reviewer,
        operation=ProposalOperation.UPDATE_VERSION,
        payload={
            "content": edited.strip(),
            "source_type": str(SourceType.USER),
            "source_reference": "edited during review",
        },
        target_memory=entity["memory_id"],
        based_on_version=entity["latest_version"],
        allow_duplicate=True,
    )
    print(f"edited    {item['title']}")


def _open_editor(text: str) -> str:
    editor = os.environ.get("MASHU_EDITOR") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        path = handle.name
    subprocess.run([*editor.split(), path], check=False)
    body = pathlib.Path(path).read_text(encoding="utf-8")
    pathlib.Path(path).unlink(missing_ok=True)
    return body


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

    s = sub.add_parser("inspect", help="show one proposal, or a whole bundle, in full")
    s.add_argument("proposal_id", nargs="?")
    s.add_argument("--bundle", help="session id prefix, or 'none', to read the whole bundle")
    s.set_defaults(func=cmd_show)

    f = sub.add_parser("find", help="run a query through the retrieval pipeline")
    f.add_argument("query")
    f.add_argument("--type", action="append", help="restrict to a memory type")
    f.add_argument("--limit", type=int, default=retrieval.DEFAULT_LIMIT)
    f.add_argument("--actor", default="user")
    f.set_defaults(func=cmd_search)

    n = sub.add_parser("bootstrap", help="show the fixed context a session starts with")
    n.add_argument("--scope", default=None, help="narrow the current state to one scope")
    n.add_argument("--actor", default="user")
    n.set_defaults(func=cmd_bootstrap)

    c = sub.add_parser("scope", help="what scopes there are, and how much is adopted")
    c.add_argument("--add", metavar="NAME", help="create a scope (7; the user's call only)")
    c.add_argument("--about", help="the one line scope detection and the session index use")
    c.add_argument("--route", metavar="PATH", help="map a directory onto it at the same time")
    c.add_argument("--actor", default="user")
    c.set_defaults(func=cmd_scope)

    ac = sub.add_parser("active", help="everything a scope holds as true (16.1, 27.4b)")
    ac.add_argument("--scope", help="name of an existing scope; omit for every scope")
    ac.add_argument("--json", action="store_true", help="the form the extraction prompt takes")
    ac.add_argument("--full", action="store_true", help="print each body as well as its title")
    ac.set_defaults(func=cmd_active)

    rm = sub.add_parser("remember", help="record something the user states (16, 17)")
    rm.add_argument("content", nargs="?", help="the body; omit when using --file")
    rm.add_argument("--file", help="read the body from a file instead")
    rm.add_argument("--scope", help="name of an existing scope; required unless --until")
    rm.add_argument("--type", choices=[str(t) for t in MemoryType])
    rm.add_argument("--title")
    rm.add_argument(
        "--until",
        metavar="WHEN",
        help="record it as a condition that expires: an ISO timestamp, or 24h / 3d / 2w (25.2)",
    )
    rm.add_argument("--kind", default="fact", choices=list(context.KINDS))
    rm.add_argument("--directive", help="the short standing form, if it is pushed later (21.2)")
    rm.add_argument("--actor", default="user")
    rm.add_argument(
        "--anyway",
        action="store_true",
        help="record it even though a similar entity exists (20)",
    )
    rm.set_defaults(func=cmd_remember)

    d = sub.add_parser("deliver", help="move a memory between push and pull (21.2)")
    d.add_argument("memory", help="memory id, or enough of its start to be unambiguous")
    d.add_argument("delivery", choices=[str(x) for x in Delivery])
    d.add_argument("--actor", default="user")
    d.set_defaults(func=cmd_deliver)

    wk = sub.add_parser("work", help="run the extraction worker over the queue (16.3)")
    wk.add_argument("--limit", type=int, default=1, help="how many runs to take this time")
    wk.add_argument("--extractor", help="api, cli:claude, cli:codex, stub, or auto")
    wk.add_argument("--dry-run", action="store_true", help="size the input without calling a model")
    wk.set_defaults(func=cmd_work)

    sw = sub.add_parser("sweep", help="enqueue transcripts no hook claimed (16.3)")
    sw.add_argument("--days", type=int, default=7, help="how far back to look")
    sw.add_argument("--limit", type=int, default=200, help="files per CLI to consider")
    sw.set_defaults(func=cmd_sweep)

    ro = sub.add_parser("route", help="map a working directory onto a scope (16.3)")
    ro.add_argument("--add", metavar="PATH", help="map this directory tree")
    ro.add_argument("--scope", help="the scope name it maps to")
    ro.add_argument("--ignore", metavar="PATH", help="map this directory to no scope, on purpose")
    ro.add_argument("--remove", metavar="PATH", help="drop the mapping for this directory")
    ro.add_argument("--actor", default="user")
    ro.set_defaults(func=cmd_route)

    re_ = sub.add_parser("retire", help="record that something is finished or wrong (16.1)")
    re_.add_argument("memory_id")
    re_.add_argument("status", choices=["completed", "disproven", "dormant"])
    re_.add_argument("--reason", required=True, help="what makes this true")
    re_.add_argument("--actor", default="user")
    re_.add_argument("--reviewer", default="user")
    re_.set_defaults(func=cmd_retire)

    rv = sub.add_parser("review", help="decide a whole bundle in one sitting (18.1)")
    rv.add_argument("--bundle", help="session id prefix, or 'none'; the oldest if omitted")
    rv.add_argument("--batch", help="the decisions, instead of being asked for them")
    rv.add_argument("--all", action="store_true", help="include items already put off")
    rv.add_argument("--reviewer", default="user")
    rv.add_argument("--reason", default=None, help="a note recorded on every approval")
    rv.set_defaults(func=cmd_review)

    st = sub.add_parser("status", help="what the system is costing and how far behind (27.3)")
    st.add_argument(
        "--days", type=int, default=metrics.WINDOW_DAYS, help="how far back the load figures look"
    )
    st.set_defaults(func=cmd_status)

    di = sub.add_parser("directive", help="write a memory's short standing form (21.2)")
    di.add_argument("memory", help="memory id, or enough of its start to be unambiguous")
    di.add_argument("text", nargs="?", help="the short form a push carries")
    di.add_argument("--clear", action="store_true", help="take the short form off again")
    di.add_argument("--actor", default="user")
    di.set_defaults(func=cmd_directive)

    v = sub.add_parser("serve", help="run the MCP server over stdio")
    v.add_argument("--agent", default=None, help="the identity proposals are recorded under")
    v.set_defaults(func=cmd_serve)

    # ----------------------------------------------------------------------
    # Maintenance, migration and evaluation (30 段 C).
    #
    # Separated from the daily surface because review is optional now, and
    # a person who has to find `review` among two dozen names weighted
    # towards one-off chores is a person who opens it less often. Nothing
    # here is rarely useful; it is rarely useful *while deciding*.
    #
    # serve, sweep and work stay above: those are the three a machine types,
    # and their names are a contract with the MCP config and the launch
    # agent, neither of which lives in this repository.
    # ----------------------------------------------------------------------
    admin = sub.add_parser("admin", help="maintenance, migration and evaluation")
    adm = admin.add_subparsers(dest="admin_command", required=True)

    q = adm.add_parser("queue", help="list proposals waiting for review, by session")
    q.set_defaults(func=cmd_queue)

    a = adm.add_parser("approve", help="approve a proposal or a whole bundle")
    a.add_argument("proposal_id", nargs="?")
    a.add_argument("--bundle", help="session id prefix, or 'none' for the unattributed bundle")
    a.add_argument("--skip", nargs="*", default=[], help="proposal ids to leave out")
    a.add_argument("--reviewer", default="user")
    a.add_argument("--reason", default=None)
    a.set_defaults(func=cmd_approve)

    r = adm.add_parser("reject", help="turn a proposal down, with a reason")
    r.add_argument("proposal_id")
    r.add_argument("--reason", required=True)
    r.add_argument("--reviewer", default="user")
    r.set_defaults(func=cmd_reject)

    i = adm.add_parser("import", help="import a JSON file of memories into a scope")
    i.add_argument("file")
    i.add_argument("--scope", required=True, help="name of an existing scope")
    i.add_argument("--actor", default="import")
    i.set_defaults(func=cmd_import)

    b = adm.add_parser("backfill", help="embed rows that have no vector yet")
    b.set_defaults(func=cmd_backfill)

    ic = sub.add_parser("incident", help="record an accident, or read the tally (27.5)")
    ic.add_argument(
        "--cause",
        choices=[str(c) for causes in incidents.CAUSES.values() for c in causes],
        help="omit to read the tally instead of writing a record",
    )
    ic.add_argument("--note", help="what happened; required when recording")
    ic.add_argument("--memory", help="the memory it was about, if there is one")
    ic.add_argument("--scope", help="the scope it happened in")
    ic.add_argument("--on", metavar="WHEN", help="when it happened, if not now")
    ic.add_argument("--days", type=int, default=None, help="how far back to read")
    ic.add_argument("--actor", default="user")
    ic.set_defaults(func=cmd_incident)

    tr = sub.add_parser("trial", help="open or close a switchover window (27.5)")
    tr.add_argument("--open", action="store_true", help="mark the window as started")
    tr.add_argument("--close", action="store_true", help="mark it as finished")
    tr.add_argument("--note", help="what is being switched off; required to open")
    tr.add_argument("--actor", default="user")
    tr.set_defaults(func=cmd_trial)

    th = adm.add_parser("thresholds", help="measure the similarity distributions (27.2)")
    th.set_defaults(func=cmd_thresholds)

    er = adm.add_parser("eval-retire", help="does the worker find what a person retires (27.4b)")
    er.add_argument(
        "--sample",
        type=int,
        default=0,
        metavar="N",
        help="also draw N adopted memories to read and confirm are still true",
    )
    er.set_defaults(func=cmd_eval_retire)

    sl = adm.add_parser("stale", help="adopted rules that name a moment in themselves (25.2)")
    sl.set_defaults(func=cmd_stale)

    m = adm.add_parser("migrate", help="apply pending migrations")
    m.set_defaults(func=cmd_migrate)

    w = adm.add_parser("preview", help="rank a scope's candidates, caps off (27.1)")
    w.add_argument("query")
    w.add_argument("--scope", required=True)
    w.add_argument("--limit", type=int, default=retrieval.DEFAULT_LIMIT)
    w.set_defaults(func=cmd_preview)

    eq = adm.add_parser("enqueue", help="claim a transcript for extraction (16.3)")
    eq.add_argument("transcript", help="path to the session transcript")
    eq.add_argument("--cli", choices=transcript.CLIS, help="which CLI produced it")
    eq.add_argument("--session", help="that CLI's session id; read from the file if omitted")
    eq.add_argument("--cwd", help="the directory it ran in; read from the file if omitted")
    eq.add_argument("--extractor-version", default=extract.EXTRACTOR_VERSION)
    eq.set_defaults(func=cmd_enqueue)

    ru = adm.add_parser("runs", help="whether automatic capture is still working (16.3)")
    ru.set_defaults(func=cmd_runs)

    ev = adm.add_parser("evidence", help="what a memory rests on, and what rests on it (19)")
    ev.add_argument("memory")
    ev.set_defaults(func=cmd_evidence)

    rt = adm.add_parser("retype", help="correct what kind of thing an entity is (8)")
    rt.add_argument("memory")
    rt.add_argument("--to", required=True, choices=[str(t) for t in MemoryType])
    rt.add_argument("--reason", required=True)
    rt.add_argument("--actor", default="user")
    rt.set_defaults(func=cmd_retype)

    rn = adm.add_parser("retitle", help="correct what an entity is called (8, 14)")
    rn.add_argument("memory")
    rn.add_argument("--to", required=True, help="the new title")
    rn.add_argument("--reason", required=True)
    rn.add_argument("--actor", default="user")
    rn.set_defaults(func=cmd_retitle)

    mg = adm.add_parser("merge", help="fold one entity into another (20.2)")
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

    t = adm.add_parser("state", help="propose a scope's current state (14)")
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

    return parser


#: Where each name went when the daily surface was separated from maintenance
#: (30 段 C). A command that stops answering without saying where it went is
#: indistinguishable from one that was taken away, and the person typing it
#: cannot tell which from the error argparse gives.
_MOVED = {
    "search": "find",
    "show": "inspect",
    **{
        name: f"admin {name}"
        for name in (
            "queue",
            "approve",
            "reject",
            "import",
            "backfill",
            "migrate",
            "preview",
            "enqueue",
            "runs",
            "evidence",
            "retype",
            "merge",
            "state",
        )
    },
}


def _moved(argv: list[str]) -> tuple[str, str] | None:
    """The subcommand someone typed, if it is one that moved.

    Reads past the global options rather than taking argv[0]: --dsn carries a
    value, and mistaking that value for the subcommand would answer about the
    wrong word.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--dsn":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return (token, _MOVED[token]) if token in _MOVED else None
    return None


def main(argv: list[str] | None = None) -> int:
    moved = _moved(list(sys.argv[1:] if argv is None else argv))
    if moved:
        print(f"'{moved[0]}' is now 'mashu {moved[1]}'", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MashuError as refusal:
        # Every one of these means the knowledge state declined to do what was
        # asked, and says why. A traceback would bury that under a stack the
        # reader cannot act on.
        print(f"{refusal}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
