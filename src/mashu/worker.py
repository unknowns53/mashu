"""The thing that runs when nobody is watching (16.3, 30 段 A).

Everything else in this package is called by somebody. This is not: it wakes
up, reads what the session-end hooks left on the ledger, and files what it
finds. That is the difference between a store somebody maintains and a store
that is maintained, and it is the whole reason the layer stopped feeling like a
step backwards from a memory that fills itself.

Three commitments hold the design together.

**A worker's death costs knowledge, never truth.** Nothing here sits between a
reader and the store. If this process never runs again, retrieval keeps
answering exactly as well as it did, the temporary context still expires on the
wall clock, and what is lost is only what would have been added. That asymmetry
is what makes it safe to run unattended at all.

**Nothing it infers falls out of the store on its own.** Its output is agent
sourced by construction (16.3), so new knowledge lands as candidates; and
retirement — which is the destructive direction — is held even where section 17
would let a task completion through, because 段 B keeps agent-inferred
retirement off the automatic line. A wrong addition is visible in a review
queue. A wrong retirement is a thing that has stopped appearing, and nobody
searches for what they have forgotten.

**It does not guess where knowledge belongs.** An unmapped working directory
stops the run and says so.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from mashu import (
    events,
    extract,
    proposals,
    resolution,
    retrieval,
    routing,
    runs,
    store,
    transcript,
)
from mashu.db import transaction
from mashu.errors import DuplicateProposalError, MashuError
from mashu.models import EventType, ProposalOperation, SourceType

#: A session with fewer real user turns than this, nothing in its scratch,
#: nothing that reads as an instruction to remember, and less than
#: SKIP_MIN_CHARS of conversation is not worth a model call. All four have to
#: hold: any one of them alone throws away sessions that said something once.
SKIP_MIN_USER_TURNS = 2
SKIP_MIN_CHARS = 800

#: Phrases that make a session worth reading whatever else is true of it. The
#: point is not to detect intent reliably — it is that an explicit request to
#: remember something should never be lost to a size heuristic.
MARKERS = (
    "覚えて",
    "記憶して",
    "記録して",
    "メモして",
    "忘れないで",
    "remember this",
    "note that",
    "keep in mind",
    "for future reference",
)

#: What the worker signs its writes with. A distinct name matters: the commit
#: gate reads the actor, and review reads it to know an unattended process filed
#: this rather than an agent in conversation with somebody.
WORKER_ACTOR = "mashu-worker"

#: Why every retirement this worker proposes is held, even the ones section 17
#: would commit. Recorded on the proposal so a reviewer sees the reason rather
#: than an unexplained hold.
RETIREMENT_HOLD = (
    "proposed by the unattended extraction worker; agent-inferred retirement "
    "does not fall on its own (30 段 B)"
)


@dataclass
class Outcome:
    """What one run did, in the words the ledger and the operator both need."""

    run_id: UUID
    state: str
    note: str = ""
    proposals_filed: int = 0
    retirements_filed: int = 0
    refused: list[str] = field(default_factory=list)

    def line(self) -> str:
        counts = f"{self.proposals_filed} proposal(s), {self.retirements_filed} retirement(s)"
        return f"{str(self.run_id)[:8]}  {self.state:<9} {counts}  {self.note}".rstrip()


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------
def process(
    cur: psycopg.Cursor,
    run: dict[str, Any],
    *,
    extractor: extract.Extractor,
    dry_run: bool = False,
) -> Outcome:
    """Take one claimed run as far as it goes, and leave the ledger true either way."""
    run_id = run["run_id"]

    path = _transcript_path(run)
    if path is None:
        return _skip(cur, run_id, "the transcript is gone from disk")

    session = transcript.read(path, source_cli=run["source_cli"])
    cwd = run.get("cwd") or session.cwd

    scope_id = routing.resolve(cur, cwd)
    if scope_id is None:
        note = f"no scope route for {cwd or 'an unrecorded directory'}"
        runs.held(cur, run_id=run_id, note=note)
        return Outcome(run_id, "held", note)

    checkpoint = runs.checkpoint_for(
        cur, source_cli=run["source_cli"], external_session_id=run["external_session_id"]
    )
    turns = session.since(checkpoint)
    scratch_items = _scratch(cur, run)
    log = transcript.render(session, turns)

    reason = _skip_reason(session, turns, scratch_items, log)
    if reason:
        return _skip(cur, run_id, reason)

    active = retrieval.active_set(cur, scope_id=scope_id)
    prompt = extract.build_prompt(log=log, scratch=scratch_items, active=active)

    estimated = retrieval.estimate_tokens(prompt)
    spent = runs.spent_today(cur)
    if spent + estimated > runs.DAILY_INPUT_BUDGET:
        note = f"daily input budget reached ({spent} spent, this one needs about {estimated})"
        runs.defer(cur, run_id=run_id, until_hours=24, note=note)
        return Outcome(run_id, "deferred", note)

    if dry_run:
        return Outcome(run_id, "dry-run", f"{estimated} input token, {len(active)} active")

    answer = extractor.run(prompt)
    result = extract.parse(answer, known_ids={row["memory_id"] for row in active})

    outcome = Outcome(run_id, "succeeded", refused=result.refused)
    filed = _file_proposals(
        cur, result, scope_id=scope_id, session=session, run=run, outcome=outcome
    )
    _link_evidence(cur, result, filed)
    _file_retirements(cur, result, session=session, outcome=outcome)

    _clear_scratch(cur, run)
    usage = getattr(extractor, "usage", {}) or {}
    runs.succeeded(
        cur,
        run_id=run_id,
        model=f"{extractor.name}/{extractor.model}",
        input_tokens=usage.get("input_tokens", estimated),
        output_tokens=usage.get("output_tokens"),
        note=_note(outcome, result),
        checkpoint=session.records,
    )
    events.record(
        cur,
        EventType.PROPOSAL_CREATED,
        WORKER_ACTOR,
        detail={
            "extraction": str(run_id),
            "scope_id": str(scope_id),
            "proposals": outcome.proposals_filed,
            "retirements": outcome.retirements_filed,
            "refused": len(result.refused),
        },
    )
    outcome.note = _note(outcome, result)
    return outcome


def _transcript_path(run: dict[str, Any]) -> pathlib.Path | None:
    raw = run.get("transcript_path")
    if not raw:
        return None
    path = pathlib.Path(raw)
    return path if path.exists() else None


def _skip(cur: psycopg.Cursor, run_id: UUID, note: str) -> Outcome:
    runs.skipped(cur, run_id=run_id, note=note)
    return Outcome(run_id, "skipped", note)


def _skip_reason(session, turns, scratch_items, log) -> str | None:
    """Whether this session is too small to be worth a model call (16.3).

    Every condition has to hold at once. Each one alone is wrong: a one-turn
    session can carry the whole point of an evening, and a long one can be
    entirely tool output.
    """
    if scratch_items:
        return None
    if session.user_turns(turns) >= SKIP_MIN_USER_TURNS:
        return None
    if len(log) >= SKIP_MIN_CHARS:
        return None
    lowered = log.lower()
    if any(marker.lower() in lowered for marker in MARKERS):
        return None
    return (
        f"nothing to read: {session.user_turns(turns)} user turn(s), "
        f"{len(log)} characters, empty scratch, no explicit marker"
    )


def _scratch(cur: psycopg.Cursor, run: dict[str, Any]) -> list[dict[str, Any]]:
    """What the session itself put down, if it was talking to the MCP server."""
    cur.execute(
        "SELECT scratch FROM agent_session WHERE source_cli = %s AND external_session_id = %s",
        (run["source_cli"], run["external_session_id"]),
    )
    row = cur.fetchone()
    return (row or {}).get("scratch") or []


def _clear_scratch(cur: psycopg.Cursor, run: dict[str, Any]) -> None:
    """Only after the extraction succeeded (25.1); the retry needs its input."""
    cur.execute(
        "UPDATE agent_session SET scratch = NULL "
        "WHERE source_cli = %s AND external_session_id = %s",
        (run["source_cli"], run["external_session_id"]),
    )


# --------------------------------------------------------------------------
# filing
# --------------------------------------------------------------------------
def _reference(session, run: dict[str, Any]) -> str:
    return f"{run['source_cli']}:{session.external_id or run['external_session_id']}"


def _file_proposals(
    cur: psycopg.Cursor,
    result: extract.Extraction,
    *,
    scope_id: UUID,
    session,
    run: dict[str, Any],
    outcome: Outcome,
) -> dict[str, UUID]:
    """File new knowledge through the ordinary proposal route (16.3).

    Not the importer. The importer skips the duplicate check and entity
    resolution because a migration knows it is copying; a worker reading a new
    session knows nothing of the kind, and skipping them is how the same fact
    arrives once a night forever.

    A title that resolves to something already there becomes a provisional
    entity rather than being dropped. Nobody is present to make section 20's
    call, and provisional is the state that exists for exactly that: the
    concept is kept, and it waits for a person instead of answering as current.
    """
    filed: dict[str, UUID] = {}
    for draft in result.proposals:
        payload = {
            "scope_id": str(scope_id),
            "type": str(draft.type),
            "title": draft.title,
            "content": draft.content,
            # Never from the model. Section 16.3 keeps every worker output on
            # the agent line: reading a user's turn proves where text was
            # written, not who wrote it, and a pasted "always do X" would pass
            # any span check.
            "source_type": str(SourceType.AGENT),
            "source_reference": _reference(session, run),
        }
        try:
            made = proposals.propose(
                cur,
                actor=WORKER_ACTOR,
                operation=ProposalOperation.CREATE,
                payload=payload,
                allow_similar=True,
            )
        except DuplicateProposalError:
            outcome.refused.append(f"already proposed: {draft.title}")
            continue
        except resolution.SimilarEntityError as failure:
            outcome.refused.append(f"similar entity, not filed: {draft.title} ({failure})")
            continue
        except MashuError as failure:
            outcome.refused.append(f"{draft.title}: {failure}")
            continue

        outcome.proposals_filed += 1
        target = made["proposal"]["target_memory"]
        if target is not None:
            filed[draft.title] = target
    return filed


def _link_evidence(cur: psycopg.Cursor, result: extract.Extraction, filed: dict[str, UUID]) -> None:
    """Join up the grounds the extraction named within its own batch (19).

    Only titles from this same session resolve. A name the batch does not
    contain is not looked up across the store, because two memories can share a
    title and an edge pointing at the wrong one is worse than no edge.
    """
    for draft in result.proposals:
        if draft.title not in filed or not draft.evidence:
            continue
        grounds = [filed[title] for title in draft.evidence if title in filed]
        grounds = [g for g in grounds if g != filed[draft.title]]
        if not grounds:
            continue
        entity = store.get_entity(cur, filed[draft.title])
        version = entity["latest_version"]
        if version:
            try:
                store.record_evidence(
                    cur, from_version=version, to_memory=grounds, actor=WORKER_ACTOR
                )
            except MashuError:
                continue


def _file_retirements(
    cur: psycopg.Cursor, result: extract.Extraction, *, session, outcome: Outcome
) -> None:
    """Propose retirement, and never let it take effect unattended (30 段 B).

    hold_for_review is passed on every one of these. It can only make the gate
    stricter, and what it stops is the one case that would otherwise fall
    through: a task completion the worker inferred, which section 17 puts on the
    automatic line because it was written for a person saying "that's done".
    """
    for draft in result.retirements:
        if draft.ambiguous:
            outcome.refused.append(f"ambiguous target, not filed: {draft.title or draft.memory_id}")
            continue
        try:
            entity = store.get_entity(cur, draft.memory_id)
        except MashuError as failure:
            outcome.refused.append(f"{draft.memory_id}: {failure}")
            continue
        if not entity["active_version"]:
            outcome.refused.append(f"nothing active to retire on {draft.title or entity['title']}")
            continue

        payload = {
            "version_id": str(entity["active_version"]),
            "status": draft.target,
            "reason": draft.reason or "observed during session end extraction",
        }
        try:
            proposals.propose(
                cur,
                actor=WORKER_ACTOR,
                operation=ProposalOperation.CHANGE_STATUS,
                payload=payload,
                target_memory=draft.memory_id,
                hold_for_review=RETIREMENT_HOLD,
            )
        except DuplicateProposalError:
            outcome.refused.append(f"retirement already proposed: {entity['title']}")
            continue
        except MashuError as failure:
            outcome.refused.append(f"{entity['title']}: {failure}")
            continue
        outcome.retirements_filed += 1


def _note(outcome: Outcome, result: extract.Extraction) -> str:
    parts = [
        f"{outcome.proposals_filed}/{len(result.proposals)} proposals",
        f"{outcome.retirements_filed}/{len(result.retirements)} retirements",
    ]
    if outcome.refused:
        parts.append(f"{len(outcome.refused)} refused")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------
def run_once(
    dsn: str | None = None,
    *,
    extractor: extract.Extractor | None = None,
    limit: int = 1,
    dry_run: bool = False,
) -> list[Outcome]:
    """Work through the queue once.

    One transaction per run, deliberately. A batch in one transaction would
    make a single unreadable transcript roll back the proposals from every
    other session in it, and the ledger would then claim they were never read.
    """
    extractor = extractor or extract.get_extractor()
    outcomes: list[Outcome] = []

    for _ in range(limit):
        with transaction(dsn) as cur:
            claimed = runs.claim(cur, limit=1)
        if not claimed:
            break
        run = claimed[0]

        try:
            with transaction(dsn) as cur:
                outcomes.append(process(cur, run, extractor=extractor, dry_run=dry_run))
        except Exception as failure:  # noqa: BLE001 - the ledger records anything
            with transaction(dsn) as cur:
                runs.failed(cur, run_id=run["run_id"], error=f"{type(failure).__name__}: {failure}")
            outcomes.append(Outcome(run["run_id"], "failed", str(failure)[:200]))

    return outcomes


# --------------------------------------------------------------------------
# the sweeper
# --------------------------------------------------------------------------
#: Where each CLI keeps its transcripts. The sweeper exists because a hook that
#: did not fire leaves no trace of not having fired, so the only way to notice
#: is to look at what is on disk against what the ledger has seen.
TRANSCRIPT_ROOTS = {
    "claude": "~/.claude/projects",
    "codex": "~/.codex/sessions",
}


def sweep(
    dsn: str | None = None,
    *,
    roots: dict[str, str] | None = None,
    since_days: int = 7,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Enqueue transcripts the hooks never claimed.

    Deliberately does not read them. Whether a transcript is worth a model is
    the worker's judgement, made once, in one place; a sweeper that also
    decided would be a second copy of that rule that nobody keeps in step.
    """
    roots = roots or TRANSCRIPT_ROOTS
    cutoff = (datetime.now() - timedelta(days=since_days)).timestamp()
    workdir = str(extract.workdir())
    found: list[dict[str, Any]] = []

    for cli, root in roots.items():
        base = pathlib.Path(root).expanduser()
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.jsonl"), key=lambda p: -p.stat().st_mtime)[:limit]:
            if path.stat().st_mtime < cutoff:
                break
            try:
                session = transcript.read(path, source_cli=cli)
            except OSError:
                continue
            # The worker's own extractions leave transcripts too. Reading those
            # would make every night's capture the next night's input.
            if session.cwd and session.cwd.startswith(workdir):
                continue
            if not session.external_id:
                continue
            with transaction(dsn) as cur:
                before = _seen(cur, cli, session.external_id, transcript.digest(path))
                if before:
                    continue
                run = runs.enqueue(
                    cur,
                    source_cli=cli,
                    external_session_id=session.external_id,
                    transcript_digest=transcript.digest(path),
                    extractor_version=extract.EXTRACTOR_VERSION,
                    transcript_path=str(path),
                    cwd=session.cwd,
                )
            found.append(run)
    return found


def _seen(cur: psycopg.Cursor, cli: str, external_id: str, digest: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM extraction_run
        WHERE source_cli = %s AND external_session_id = %s AND transcript_digest = %s
          AND extractor_version = %s
        """,
        (cli, external_id, digest, extract.EXTRACTOR_VERSION),
    )
    return cur.fetchone() is not None
