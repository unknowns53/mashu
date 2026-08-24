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

#: The most a single model call is given (16.3). A whole evening's transcript
#: runs to hundreds of thousands of tokens, and paying that in one call is the
#: naive cost the section refuses. So a long session is read in windows at turn
#: boundaries, the ledger's checkpoint is the cursor, and the run comes back for
#: the next window instead of the whole thing arriving at once. Nothing is
#: dropped: what is not read this pass is read on the next one.
MAX_INPUT_TOKENS = 60_000

#: Phrases that make a session worth reading whatever else is true of it. The
#: point is not to detect intent reliably — it is that an explicit request to
#: remember something should never be lost to a size heuristic.
#: Deliberately narrow. "note that" and "keep in mind" are ordinary English
#: technical chatter, and including them turned the size heuristic off for every
#: English session — every two-line exchange bought a model call.
MARKERS = (
    "覚えて",
    "記憶して",
    "記録して",
    "メモして",
    "忘れないで",
    "remember this",
    "remember that",
    "for future reference",
)

#: How much of the session either side of a flag the extraction reads (16.3).
#:
#: This is the contract scratch was always meant to be, and the only thing on
#: the table that changes the cost by an order rather than by a half. Folding
#: repeats took a real evening from 68万 to 36万 token; the unique prose alone
#: is 165k, so no way of rearranging or trimming a transcript reaches a tenth.
#: Reading a fraction does, and the only defensible fraction is the one the
#: session itself pointed at while it was running.
#:
#: More behind than ahead: a note is written after the thing it is about.
SCRATCH_BEFORE = 6
SCRATCH_AFTER = 2

#: What the worker signs its writes with. A distinct name matters: the commit
#: gate reads the actor, and review reads it to know an unattended process filed
#: this rather than an agent in conversation with somebody.
WORKER_ACTOR = "mashu-worker"

#: Why every retirement this worker proposes is held, even the ones section 17
#: would commit. Recorded on the proposal so a reviewer sees the reason rather
#: than an unexplained hold.
#: How long a rejection keeps the worker from proposing the same thing again.
#:
#: Section 15.1's duplicate check was written for a proposer that can look at
#: what was already ruled on and decide. The worker cannot look, so for it the
#: check is a permanent, silent ban: one rejected title suppresses everything
#: near it in that scope forever, and a retirement turned down as premature can
#: never be proposed again even once the task genuinely finishes.
#:
#: So a rejection expires as a bar, not as a fact. After the horizon the worker
#: may raise it again, and it carries the earlier verdict into the new proposal
#: so the reviewer meets an argument rather than a repetition.
REJECTION_HORIZON_DAYS = 30

RETIREMENT_HOLD = (
    "proposed by the unattended extraction worker; agent-inferred retirement "
    "does not fall on its own (30 段 B)"
)


@dataclass
class Plan:
    """Everything a model call needs, gathered before the call and held plainly.

    The point of it being a value rather than a cursor is that the model call
    happens between two transactions instead of inside one. A call can take ten
    minutes; a transaction open that long holds the row locks this took, blocks
    a live session writing its own scratch, and on most managed databases is
    killed by an idle-in-transaction timeout — a failure indistinguishable from
    the model having gone wrong.
    """

    run: dict[str, Any]
    scope_id: UUID
    session: Any
    turns: list
    more: bool
    records: int
    session_id: UUID
    active_ids: set[UUID]
    prompt: str
    estimated: int
    #: Which of the two readings this is, and how much of what was unread it
    #: covered. Recorded rather than judged: whether reading a fraction costs
    #: recall is a question for 27.4, and it cannot be asked of runs that did
    #: not say which they were.
    mode: str = "log"
    covered: int = 0
    unread: int = 0
    #: The rows the scratch came from, and how they were found. Which of the
    #: two ways matters: an id match means the CLI never rotated the session,
    #: and a place match means it did and the fallback carried the run.
    scratch_from: list = field(default_factory=list)
    found_by: str = "none"


@dataclass
class Outcome:
    """What one run did, in the words the ledger and the operator both need."""

    run_id: UUID
    state: str
    note: str = ""
    proposals_filed: int = 0
    updates_filed: int = 0
    retirements_filed: int = 0
    refused: list[str] = field(default_factory=list)

    def line(self) -> str:
        counts = (
            f"{self.proposals_filed} new, {self.updates_filed} update(s), "
            f"{self.retirements_filed} retirement(s)"
        )
        return f"{str(self.run_id)[:8]}  {self.state:<9} {counts}  {self.note}".rstrip()


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------
def prepare(cur: psycopg.Cursor, run: dict[str, Any], *, dry_run: bool = False) -> Plan | Outcome:
    """Everything before the model call. Returns an Outcome when it stops here."""
    run_id = run["run_id"]

    path = _transcript_path(run)
    if path is None:
        return _skip(cur, run_id, "the transcript is gone from disk")

    session = transcript.read(path, source_cli=run["source_cli"])
    cwd = run.get("cwd") or session.cwd

    scope_id, ignored = routing.resolve(cur, cwd)
    if ignored:
        return _skip(cur, run_id, f"{cwd} is mapped to no scope on purpose")
    if scope_id is None:
        note = f"no scope route for {cwd or 'an unrecorded directory'}"
        runs.held(cur, run_id=run_id, note=note)
        return Outcome(run_id, "held", note)

    checkpoint = runs.checkpoint_for(
        cur, source_cli=run["source_cli"], external_session_id=run["external_session_id"]
    )
    remaining = session.since(checkpoint)

    # Nothing new. This happens whenever a transcript's bytes change without
    # the conversation growing — the CLI appending a title or a summary after
    # the hook fired — and the sweeper then enqueues it under a new digest.
    # Without this the run builds a prompt of the whole active set around an
    # empty log and asks a model to retire things on no evidence, once per
    # session, forever.
    if checkpoint and not remaining:
        return _skip(cur, run_id, f"nothing new since turn {checkpoint}")

    scratch_items, scratch_from, found_by = _scratch(cur, run, session)
    turns, more, mode = _reading(session, remaining, scratch_items)

    # Only on the first pass. A session already judged worth reading must not
    # be abandoned half way through because its second window happens to be
    # short — that would drop the end of every long session it applied to.
    if not checkpoint:
        reason = _skip_reason(session, remaining, scratch_items)
        if reason:
            return _skip(cur, run_id, reason)

    internal = _session(cur, run)
    log = transcript.render(session, turns)
    active = retrieval.active_set(cur, scope_id=scope_id)
    prompt = extract.build_prompt(log=log, scratch=scratch_items, active=active)
    estimated = retrieval.estimate_tokens(prompt)

    spent = runs.spent_today(cur)
    if spent + estimated > runs.DAILY_INPUT_BUDGET:
        note = f"daily input budget reached ({spent} spent, this one needs about {estimated})"
        runs.defer(cur, run_id=run_id, note=note)
        return Outcome(run_id, "deferred", note)

    if dry_run:
        runs.unclaim(cur, run_id=run_id)
        return Outcome(
            run_id,
            "dry-run",
            f"{estimated} input token, {len(active)} active, "
            f"{mode} reading {len(turns)} of {len(remaining)} turn(s)",
        )

    return Plan(
        run=run,
        scope_id=scope_id,
        session=session,
        turns=turns,
        more=more,
        records=session.records,
        session_id=internal["session_id"],
        active_ids={row["memory_id"] for row in active},
        prompt=prompt,
        estimated=estimated,
        mode=mode,
        covered=len(turns),
        unread=len(remaining),
        scratch_from=scratch_from,
        found_by=found_by,
    )


def land(cur: psycopg.Cursor, plan: Plan, answer: str, *, extractor: extract.Extractor) -> Outcome:
    """Everything after the model call: check the answer, file it, close the run."""
    run, run_id = plan.run, plan.run["run_id"]
    result = extract.parse(answer, known_ids=plan.active_ids)

    outcome = Outcome(run_id, "succeeded", refused=result.refused)
    filed = _file_proposals(
        cur,
        result,
        scope_id=plan.scope_id,
        session=plan.session,
        run=run,
        outcome=outcome,
        session_id=plan.session_id,
    )
    _link_evidence(cur, result, filed, outcome)
    _file_updates(
        cur, result, session=plan.session, run=run, outcome=outcome, session_id=plan.session_id
    )
    _file_retirements(cur, result, outcome=outcome, session_id=plan.session_id)

    usage = getattr(extractor, "usage", {}) or {}
    runs.spend(
        cur,
        run_id=run_id,
        input_tokens=usage.get("input_tokens", plan.estimated),
        output_tokens=usage.get("output_tokens"),
    )
    runs.record_dropped(cur, run_id=run_id, dropped=result.scratch)
    read_to = plan.turns[-1].ordinal if plan.turns else plan.records

    if plan.more:
        # More of this session than one call may hold. The mark moves to the end
        # of what was read and the run comes back for the rest, so the cost per
        # call stays bounded without any of the session going unread.
        runs.advance(cur, run_id=run_id, checkpoint=read_to)
        runs.defer(
            cur,
            run_id=run_id,
            until_hours=0,
            note=f"{_note(outcome, result, plan)}; read to turn {read_to}, more to come",
        )
        outcome.state = "windowed"
        outcome.note = f"{_note(outcome, result, plan)}; read to turn {read_to} of {plan.records}"
        return outcome

    _clear_scratch(cur, plan.scratch_from)
    runs.succeeded(
        cur,
        run_id=run_id,
        model=f"{extractor.name}/{extractor.model}",
        note=_note(outcome, result, plan),
        checkpoint=plan.records,
    )
    events.record(
        cur,
        EventType.EXTRACTION_FILED,
        WORKER_ACTOR,
        detail={
            "extraction": str(run_id),
            "scope_id": str(plan.scope_id),
            "proposals": outcome.proposals_filed,
            "updates": outcome.updates_filed,
            "retirements": outcome.retirements_filed,
            "refused": len(result.refused),
            "reading": plan.mode,
            "turns_read": plan.covered,
            "turns_unread": plan.unread,
            "scratch_found_by": plan.found_by,
        },
    )
    outcome.note = _note(outcome, result, plan)
    return outcome


def process(
    cur: psycopg.Cursor,
    run: dict[str, Any],
    *,
    extractor: extract.Extractor,
    dry_run: bool = False,
) -> Outcome:
    """Both halves against one cursor. For a single manual run, and for tests.

    run_once is what the unattended path uses, and it deliberately does not go
    through here: the model call belongs between transactions, not inside one.
    """
    plan = prepare(cur, run, dry_run=dry_run)
    if isinstance(plan, Outcome):
        return plan
    return land(cur, plan, extractor.run(plan.prompt), extractor=extractor)


def _reading(session, turns: list, scratch_items: list[dict[str, Any]]) -> tuple[list, bool, str]:
    """Which turns this call reads, and under which of the two contracts (16.3).

    Section 16.3 named scratch the first input and the log the fallback, and
    the fallback is what has been running: the motivation was written in a tool
    description nobody is shown, so nothing was ever flagged, and reading the
    whole transcript was the only thing left. Wiring the motivation is only
    half of it — the cost has to actually follow the flags, or an agent that
    complies pays exactly what one that does not pays.

    So a session that flagged something is read around its flags. A session
    that flagged nothing is read the way it is read today. That is the shape
    the failure has to take: complying is cheap, not complying is unchanged,
    and neither is silently worse.

    What the fraction costs is real and is not hidden. Turns outside the
    neighbourhoods are not read, and the checkpoint moves past them, so a
    session that flagged two things in an evening has an evening go by on two
    flags. The ledger records which reading ran and how much it covered,
    because that is the only way to find out whether the contract holds.
    """
    marked = _flagged(turns, scratch_items)
    if marked:
        chosen, more = _window(session, marked)
        return chosen, more, "scratch"
    return (*_window(session, turns), "log")


def _flagged(turns: list, scratch_items: list[dict[str, Any]]) -> list:
    """The turns around what this session pointed at, if it pointed at anything.

    A marker is not a flag on its own. An explicit request to remember, inside
    a session that put nothing down, is a reason to read the whole log rather
    than a licence to read a fraction of it — the fraction is only defensible
    because the session pointed at it, and a session that pointed at nothing
    has not. Inside a session that did flag things, a marker is added to what
    is read, because the one case worse than reading everything is skipping the
    turn where the user said the words out loud.
    """
    if not scratch_items:
        return []
    moments = [_moment(item.get("created_at")) for item in scratch_items]
    moments += [turn.at for turn in turns if turn.at and _marked(turn.text)]
    moments = [moment for moment in moments if moment is not None]
    if not moments:
        return []
    return transcript.around(turns, moments, before=SCRATCH_BEFORE, after=SCRATCH_AFTER)


def _marked(text: str) -> bool:
    body = text.lower()
    return any(marker.lower() in body for marker in MARKERS)


def _moment(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.astimezone()
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def _window(session, turns: list) -> tuple[list, bool]:
    """As many turns from the front as one call may hold, and whether more remain.

    From the front, not the back. Reading only the tail would be cheaper and
    would quietly lose the beginning of every long session, which is where the
    decisions that the rest of the evening rests on are usually made.

    Priced on what the log will actually say, not on the raw turn: a repeat is
    rendered as a place-holder, and charging it the full body would fill the
    budget with text nobody sends and split the session into twice the calls.
    """
    budget = MAX_INPUT_TOKENS
    blocks = session.blocks()
    taken: list = []
    for turn in turns:
        cost = retrieval.estimate_tokens(blocks[turn.ordinal])
        if taken and budget - cost < 0:
            return taken, True
        budget -= cost
        taken.append(turn)
    return taken, False


def _transcript_path(run: dict[str, Any]) -> pathlib.Path | None:
    raw = run.get("transcript_path")
    if not raw:
        return None
    path = pathlib.Path(raw)
    return path if path.exists() else None


def _skip(cur: psycopg.Cursor, run_id: UUID, note: str) -> Outcome:
    runs.skipped(cur, run_id=run_id, note=note)
    return Outcome(run_id, "skipped", note)


def _skip_reason(session, turns, scratch_items) -> str | None:
    """Whether this session is too small to be worth a model call (16.3).

    Judged on everything still unread, never on the window one call happens to
    take. The window is a cost decision; this is a worth-reading decision, and
    conflating them would abandon a long session whose next window is short.

    Every condition has to hold at once. Each one alone is wrong: a one-turn
    session can carry the whole point of an evening, and a long one can be
    entirely tool output.
    """
    if scratch_items:
        return None
    if session.user_turns(turns) >= SKIP_MIN_USER_TURNS:
        return None
    size = sum(len(turn.text) for turn in turns)
    if size >= SKIP_MIN_CHARS:
        return None
    if any(_marked(turn.text) for turn in turns):
        return None
    return (
        f"nothing to read: {session.user_turns(turns)} user turn(s), "
        f"{size} characters, empty scratch, no explicit marker"
    )


def _session(cur: psycopg.Cursor, run: dict[str, Any]) -> dict[str, Any]:
    """The internal session row for the CLI session this transcript came from.

    Created if the session never spoke to the MCP server, which is most of
    them. Section 18.1 reviews a bundle at a time and the bundle is a session,
    so proposals filed without one land in the unnamed heap together with every
    other night's — the reviewer then rebuilds context per item, which is the
    cost the bundle exists to pay once.
    """
    cur.execute(
        """
        INSERT INTO agent_session (agent, source_cli, external_session_id, transcript_digest)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (source_cli, external_session_id) WHERE external_session_id IS NOT NULL
        DO UPDATE SET transcript_digest = EXCLUDED.transcript_digest
        RETURNING *
        """,
        (
            run["source_cli"],
            run["source_cli"],
            run["external_session_id"],
            run["transcript_digest"],
        ),
    )
    return cur.fetchone()


def _scratch(cur: psycopg.Cursor, run: dict[str, Any], session) -> tuple[list, list, str]:
    """What this session put down, and which rows it came from (0020).

    By id where the id still holds. It often does not: the MCP server reads its
    session id from the environment it started in, and a CLI issues a new one
    when a conversation is compacted or resumed without restarting the server,
    so from that moment the notes are written under the previous id while the
    transcript being extracted carries the current one. Nothing in the
    transcript joins the two, and the sessions this happens to are the long
    ones — which are exactly the ones worth not reading in full.

    So by place and time when the id misses. A session was in a directory and
    its notes carry the moment each was written; the transcript names the same
    directory and covers a stretch of clock. Two sessions in one directory at
    once will each see the other's notes, which costs a few extra turns read
    and a stray line in the prompt. The alternative was missing every compacted
    session in silence.
    """
    cur.execute(
        "SELECT session_id, scratch FROM agent_session "
        "WHERE source_cli = %s AND external_session_id = %s",
        (run["source_cli"], run["external_session_id"]),
    )
    row = cur.fetchone()
    if row and row["scratch"]:
        return row["scratch"], [row["session_id"]], "id"

    cwd = run.get("cwd") or session.cwd
    moments = [turn.at for turn in session.turns if turn.at]
    if not cwd or not moments:
        return [], [], "none"
    first, last = min(moments), max(moments)

    cur.execute(
        "SELECT session_id, scratch FROM agent_session "
        "WHERE cwd = %s AND coalesce(jsonb_array_length(scratch), 0) > 0",
        (cwd,),
    )
    items: list = []
    sources: list = []
    for other in cur.fetchall():
        within = [
            item
            for item in other["scratch"]
            if (at := _moment(item.get("created_at"))) is not None and first <= at <= last
        ]
        if within:
            items += within
            sources.append(other["session_id"])
    return items, sources, ("place" if items else "none")


def _clear_scratch(cur: psycopg.Cursor, session_ids: list) -> None:
    """Only after the extraction succeeded (25.1); the retry needs its input.

    Clears the rows the notes actually came from rather than the row this
    transcript's id names, because after a compaction those are not the same
    row and clearing the wrong one leaves the notes to be read again forever.
    """
    if not session_ids:
        return
    cur.execute(
        "UPDATE agent_session SET scratch = NULL WHERE session_id = ANY(%s)", (session_ids,)
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
    session_id: UUID,
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
            # Kept because the prompt asks for them and review needs them: the
            # rationale says which of the three tests the extraction thought
            # this passed, and duplicates names the item in the old notes this
            # may be a migration of rather than a discovery. Dropping them here
            # means paying output tokens for fields with no reader.
            "rationale": draft.rationale or None,
            "duplicates": draft.duplicates or None,
            "claimed_gate": draft.claimed_gate,
        }
        try:
            made = proposals.propose(
                cur,
                actor=WORKER_ACTOR,
                operation=ProposalOperation.CREATE,
                payload=payload,
                session_id=session_id,
                allow_similar=True,
            )
        except DuplicateProposalError as clash:
            earlier = _stale_rejection(clash)
            if earlier is None:
                outcome.refused.append(f"already proposed: {draft.title}")
                continue
            made = proposals.propose(
                cur,
                actor=WORKER_ACTOR,
                operation=ProposalOperation.CREATE,
                payload=dict(payload, previously_rejected=earlier),
                session_id=session_id,
                allow_similar=True,
                allow_duplicate=True,
            )
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


def _link_evidence(
    cur: psycopg.Cursor,
    result: extract.Extraction,
    filed: dict[str, UUID],
    outcome: Outcome | None = None,
) -> None:
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
            except MashuError as failure:
                if outcome is not None:
                    outcome.refused.append(f"grounds not linked for {draft.title}: {failure}")


def _file_updates(
    cur: psycopg.Cursor,
    result: extract.Extraction,
    *,
    session,
    run: dict[str, Any],
    outcome: Outcome,
    session_id: UUID,
) -> None:
    """Add a corrected version to an entity that already exists (20).

    This is the operation the layer is for, and the worker had no way to say
    it. Without it a session that refines a standing memory can only make a
    rival under another title — which splits the knowledge — or a near
    duplicate that hangs unresolved until somebody tells the two apart.

    It lands as a candidate on the existing entity, so the correction shows up
    in layer 2 beside the thing it corrects, and the active pointer does not
    move until a person moves it.
    """
    for draft in result.updates:
        try:
            entity = store.get_entity(cur, draft.memory_id)
        except MashuError as failure:
            outcome.refused.append(f"{draft.memory_id}: {failure}")
            continue
        if entity["active_version"] is None:
            outcome.refused.append(f"nothing active to correct on {entity['title']}")
            continue
        if store.get_version(cur, entity["active_version"])["content"].strip() == (
            draft.content.strip()
        ):
            outcome.refused.append(f"unchanged: {entity['title']}")
            continue

        try:
            proposals.propose(
                cur,
                actor=WORKER_ACTOR,
                operation=ProposalOperation.UPDATE_VERSION,
                payload={
                    "content": draft.content,
                    "source_type": str(SourceType.AGENT),
                    "source_reference": _reference(session, run),
                    "rationale": draft.reason or None,
                },
                target_memory=entity["memory_id"],
                based_on_version=entity["latest_version"],
                session_id=session_id,
            )
        except DuplicateProposalError:
            outcome.refused.append(f"correction already proposed: {entity['title']}")
            continue
        except MashuError as failure:
            outcome.refused.append(f"{entity['title']}: {failure}")
            continue
        outcome.updates_filed += 1


def _file_retirements(
    cur: psycopg.Cursor,
    result: extract.Extraction,
    *,
    outcome: Outcome,
    session_id: UUID,
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
                session_id=session_id,
                hold_for_review=RETIREMENT_HOLD,
            )
        except DuplicateProposalError as clash:
            earlier = _stale_rejection(clash)
            if earlier is None:
                outcome.refused.append(f"retirement already proposed: {entity['title']}")
                continue
            proposals.propose(
                cur,
                actor=WORKER_ACTOR,
                operation=ProposalOperation.CHANGE_STATUS,
                payload=dict(payload, previously_rejected=earlier),
                target_memory=draft.memory_id,
                session_id=session_id,
                allow_duplicate=True,
                hold_for_review=RETIREMENT_HOLD,
            )
        except MashuError as failure:
            outcome.refused.append(f"{entity['title']}: {failure}")
            continue
        outcome.retirements_filed += 1


def _stale_rejection(clash: DuplicateProposalError) -> dict[str, Any] | None:
    """Whether an old rejection has stopped being a bar (see REJECTION_HORIZON_DAYS).

    Nothing is unblocked while a proposal is still waiting: a pending item is a
    reviewer who has not looked yet, and proposing beside it doubles their work
    rather than informing it.
    """
    if any(row["status"] == "pending" for row in clash.existing):
        return None
    turned_down = clash.rejected
    if not turned_down:
        return None

    latest = max(turned_down, key=lambda row: row["decided_at"] or datetime.min)
    decided = latest["decided_at"]
    if decided is None:
        return None
    age = datetime.now(decided.tzinfo) - decided
    if age < timedelta(days=REJECTION_HORIZON_DAYS):
        return None
    return {
        "decided_at": decided.isoformat(),
        "reason": latest["decision_reason"],
        "proposal_id": str(latest["proposal_id"]),
    }


def _note(outcome: Outcome, result: extract.Extraction, plan: Plan | None = None) -> str:
    parts = []
    if plan is not None and plan.mode == "scratch":
        parts.append(f"read around {plan.covered} of {plan.unread} flagged turn(s)")
    parts += [
        f"{outcome.proposals_filed}/{len(result.proposals)} proposals",
        f"{outcome.updates_filed}/{len(result.updates)} updates",
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

    Three transactions per run, and the model call in none of them. A call can
    take ten minutes; a transaction open across it holds the locks the claim
    took, blocks a live session writing its own scratch, and on most managed
    databases is killed by an idle-in-transaction timeout — a death that looks
    exactly like the model having failed. What makes the gap safe is the claim
    itself: the run is marked running with the moment it was taken, so no other
    worker picks it up while this one is thinking.

    One run per transaction, too. A batch in one would make a single unreadable
    transcript roll back the proposals from every other session in it, and the
    ledger would then say they were never read.
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
                plan = prepare(cur, run, dry_run=dry_run)
            if isinstance(plan, Outcome):
                outcomes.append(plan)
                continue

            answer = extractor.run(plan.prompt)

            with transaction(dsn) as cur:
                outcomes.append(land(cur, plan, answer, extractor=extractor))
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

#: Path components whose files are not sessions in their own right. A subagent
#: transcript is a sidechain of the session that spawned it: the reader drops
#: those records, so the file arrives empty, and enqueueing it buys a ledger row
#: and a skip for something that was never going to be read. What the subagent
#: concluded reaches the extraction through its parent's transcript.
NOT_A_SESSION = ("subagents",)


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
            if any(part in NOT_A_SESSION for part in path.parts):
                continue
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
