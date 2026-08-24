"""Proposals, the three commit lines, and the review queue.

Specifications 15, 15.1, 17 and 18.1.
"""

from __future__ import annotations

import uuid

import pytest

from mashu import proposals, store
from mashu.errors import DuplicateProposalError, ProposalError
from mashu.gate import CommitDecision
from mashu.models import (
    EntityStatus,
    MemoryType,
    ProposalOperation,
    ProposalStatus,
    SourceType,
    VersionStatus,
)
from mashu.resolution import SimilarEntityError


def _create_payload(scope_id, **over):
    payload = {
        "scope_id": str(scope_id),
        "type": str(MemoryType.FACT),
        "title": "the runner returns a failing code while the body completes",
        "content": "observed twice on the same harness",
        "source_type": str(SourceType.AGENT),
    }
    payload.update(over)
    return payload


def _propose_create(cur, scope_id, allow_duplicate=False, allow_similar=False, **over):
    return proposals.propose(
        cur,
        actor="agent-1",
        operation=ProposalOperation.CREATE,
        payload=_create_payload(scope_id, **over),
        allow_duplicate=allow_duplicate,
        allow_similar=allow_similar,
    )


# --------------------------------------------------------------------------
# the candidate line
# --------------------------------------------------------------------------
def test_candidate_is_written_but_not_adopted(cur, scope_id):
    """Layer 2 needs the content to exist while the review is outstanding."""
    result = _propose_create(cur, scope_id)
    assert result["ruling"].decision is CommitDecision.CANDIDATE

    proposal = proposals.get(cur, result["proposal"]["proposal_id"])
    assert proposal["status"] == ProposalStatus.PENDING
    assert proposal["applied_version"] is not None

    version = store.get_version(cur, proposal["applied_version"])
    assert version["status"] == VersionStatus.CANDIDATE

    entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] is None
    assert entity["latest_version"] == proposal["applied_version"]


def test_approving_a_candidate_adopts_it(cur, scope_id):
    result = _propose_create(cur, scope_id)
    pid = result["proposal"]["proposal_id"]

    decided = proposals.approve(cur, pid, reviewer="user", reason="checked the harness log")
    assert decided["status"] == ProposalStatus.APPROVED

    entity = store.get_entity(cur, decided["target_memory"])
    assert entity["active_version"] == decided["applied_version"]


def test_rejecting_marks_the_candidate_rejected_rather_than_deleting_it(cur, scope_id):
    """Deleting it would let the same proposal come straight back.

    rejected and not dormant: dormant is a reading of the content, and being
    turned down says nothing about whether the content might one day hold.
    """
    result = _propose_create(cur, scope_id)
    pid = result["proposal"]["proposal_id"]

    decided = proposals.reject(cur, pid, reviewer="user", reason="this was the old harness")
    assert decided["status"] == ProposalStatus.REJECTED
    assert decided["decision_reason"]

    version = store.get_version(cur, decided["applied_version"])
    assert version["status"] == VersionStatus.REJECTED
    assert version["reason"] == "this was the old harness"


def test_rejection_needs_a_reason(cur, scope_id):
    pid = _propose_create(cur, scope_id)["proposal"]["proposal_id"]
    with pytest.raises(ProposalError):
        proposals.reject(cur, pid, reviewer="user", reason="")


def test_a_decided_proposal_cannot_be_decided_again(cur, scope_id):
    pid = _propose_create(cur, scope_id)["proposal"]["proposal_id"]
    proposals.approve(cur, pid, reviewer="user")
    with pytest.raises(ProposalError):
        proposals.reject(cur, pid, reviewer="user", reason="changed my mind")


# --------------------------------------------------------------------------
# the auto line
# --------------------------------------------------------------------------
def test_a_preference_the_user_stated_is_applied_without_review(cur, scope_id):
    """The source is what makes it automatic, not the type (17).

    A preference governs every later session, so the agent guessing at one is
    an interpretation and waits like any other; the user saying one is the
    user changing their own setting.
    """
    result = _propose_create(
        cur,
        scope_id,
        type=str(MemoryType.PREFERENCE),
        title="commit messages in English",
        source_type=str(SourceType.USER),
    )
    assert result["ruling"].decision is CommitDecision.AUTO

    proposal = result["proposal"]
    assert proposal["status"] == ProposalStatus.AUTO_COMMITTED
    assert proposal["decided_at"] is not None
    assert proposal["reviewer"] is None

    entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] == proposal["applied_version"]


def test_auto_committed_work_does_not_enter_the_queue(cur, scope_id):
    _propose_create(
        cur,
        scope_id,
        type=str(MemoryType.PREFERENCE),
        title="a preference",
        source_type=str(SourceType.USER),
    )
    assert proposals.session_queue(cur) == []


def test_a_preference_the_agent_inferred_waits_like_anything_else(cur, scope_id):
    result = _propose_create(
        cur, scope_id, type=str(MemoryType.PREFERENCE), title="a preference the agent guessed at"
    )
    assert result["ruling"].decision is CommitDecision.CANDIDATE
    assert result["proposal"]["status"] == ProposalStatus.PENDING
    assert store.get_entity(cur, result["proposal"]["target_memory"])["active_version"] is None


# --------------------------------------------------------------------------
# the human review line
# --------------------------------------------------------------------------
def test_provisional_entity_is_created_but_stays_out_of_layer_one(cur, scope_id):
    """The agent is not blocked; the entity just does not answer as current."""
    result = _propose_create(cur, scope_id, entity_status=str(EntityStatus.PROVISIONAL))
    assert result["ruling"].decision is CommitDecision.HUMAN_REVIEW

    entity = store.get_entity(cur, result["proposal"]["target_memory"])
    assert entity["status"] == EntityStatus.PROVISIONAL
    assert entity["active_version"] is None


def test_approving_a_provisional_entity_settles_it(cur, scope_id):
    result = _propose_create(cur, scope_id, entity_status=str(EntityStatus.PROVISIONAL))
    pid = result["proposal"]["proposal_id"]
    decided = proposals.approve(cur, pid, reviewer="user", reason="genuinely a different thing")

    entity = store.get_entity(cur, decided["target_memory"])
    assert entity["status"] == EntityStatus.ACTIVE
    assert entity["active_version"] == decided["applied_version"]


def test_disproving_writes_nothing_until_approved(cur, scope_id):
    seed = _propose_create(cur, scope_id)["proposal"]
    version_id = seed["applied_version"]

    result = proposals.propose(
        cur,
        actor="agent-1",
        operation=ProposalOperation.CHANGE_STATUS,
        target_memory=seed["target_memory"],
        payload={
            "version_id": str(version_id),
            "status": str(VersionStatus.DISPROVEN),
            "reason": "the second run was a different binary",
        },
        allow_duplicate=True,
    )
    assert result["ruling"].decision is CommitDecision.HUMAN_REVIEW
    assert store.get_version(cur, version_id)["status"] == VersionStatus.CANDIDATE

    proposals.approve(cur, result["proposal"]["proposal_id"], reviewer="user")
    assert store.get_version(cur, version_id)["status"] == VersionStatus.DISPROVEN


# --------------------------------------------------------------------------
# 15.1 duplicate check
# --------------------------------------------------------------------------
def test_a_second_proposal_on_the_same_target_shows_the_first(cur, scope_id):
    seed = _propose_create(cur, scope_id)["proposal"]

    with pytest.raises(DuplicateProposalError) as caught:
        proposals.propose(
            cur,
            actor="agent-2",
            operation=ProposalOperation.CHANGE_STATUS,
            target_memory=seed["target_memory"],
            payload={
                "version_id": str(seed["applied_version"]),
                "status": str(VersionStatus.DORMANT),
                "reason": "stale",
            },
        )
    existing = caught.value.existing
    assert [row["proposal_id"] for row in existing] == [seed["proposal_id"]]
    assert existing[0]["days_pending"] == 0


def test_the_proposer_may_look_and_proceed(cur, scope_id):
    """15.1 is about showing the queue, not forbidding the write."""
    seed = _propose_create(cur, scope_id)["proposal"]
    second = proposals.propose(
        cur,
        actor="agent-2",
        operation=ProposalOperation.CHANGE_STATUS,
        target_memory=seed["target_memory"],
        payload={
            "version_id": str(seed["applied_version"]),
            "status": str(VersionStatus.DORMANT),
            "reason": "stale",
        },
        allow_duplicate=True,
    )
    assert second["proposal"]["status"] == ProposalStatus.PENDING


def test_a_repeated_creation_is_caught_by_title(cur, scope_id):
    _propose_create(cur, scope_id)
    with pytest.raises(DuplicateProposalError):
        _propose_create(cur, scope_id)


def test_a_different_title_is_not_a_duplicate(cur, scope_id):
    _propose_create(cur, scope_id)
    result = _propose_create(cur, scope_id, title="something else entirely")
    assert result["proposal"]["status"] == ProposalStatus.PENDING


def test_a_proposal_already_turned_down_comes_back_with_the_reason(cur, scope_id):
    """A rejection the proposer never hears about is one it walks into again.

    The reviewer already spent the minutes on this once. Showing only pending
    proposals would have let the same idea return the moment the queue cleared.
    """
    seed = _propose_create(cur, scope_id)["proposal"]
    proposals.reject(
        cur,
        seed["proposal_id"],
        reviewer="user",
        reason="the harness moved to the new fixture in week 2",
    )

    with pytest.raises(DuplicateProposalError) as caught:
        _propose_create(cur, scope_id)

    turned_down = caught.value.rejected
    assert [row["proposal_id"] for row in turned_down] == [seed["proposal_id"]]
    assert turned_down[0]["decision_reason"] == "the harness moved to the new fixture in week 2"
    assert "week 2" in str(caught.value)


def test_the_proposer_may_look_at_the_rejection_and_proceed(cur, scope_id):
    """Same as for a pending duplicate: the check informs, it does not forbid.

    Waving the duplicate check through does not wave entity resolution through
    with it. The rejected entity is still in the scope, so section 20 stops the
    caller a second time on a different question: not "did you already propose
    this" but "does this concept already exist".
    """
    seed = _propose_create(cur, scope_id)["proposal"]
    proposals.reject(cur, seed["proposal_id"], reviewer="user", reason="not this run")

    with pytest.raises(SimilarEntityError):
        _propose_create(cur, scope_id, allow_duplicate=True)

    again = _propose_create(cur, scope_id, allow_duplicate=True, allow_similar=True)
    assert again["proposal"]["status"] == ProposalStatus.PENDING


def test_no_one_can_propose_a_rejection(cur, scope_id):
    """rejected is what review does, so asking for it is asking to be turned down.

    The two retirement readings stay open to the proposer; it is only the
    verdict on the procedure that is not the proposer's to write.
    """
    seed = _propose_create(cur, scope_id)["proposal"]
    with pytest.raises(ProposalError, match="not a status anything can propose"):
        proposals.propose(
            cur,
            actor="agent-2",
            operation=ProposalOperation.CHANGE_STATUS,
            target_memory=seed["target_memory"],
            payload={
                "version_id": str(seed["applied_version"]),
                "status": str(VersionStatus.REJECTED),
                "reason": "I would rather this went away",
            },
            allow_duplicate=True,
        )


def test_a_rejected_candidate_is_not_a_re_evaluation_candidate(cur, scope_id):
    """The reason the state had to be its own: a dormant sweep must stay clean.

    Section 16.1 will look for dormant versions worth revisiting. Rejections
    are the high-volume event in this system, so parking them in dormant would
    have buried the few real ones.
    """
    seed = _propose_create(cur, scope_id)["proposal"]
    proposals.reject(cur, seed["proposal_id"], reviewer="user", reason="not this run")

    cur.execute(
        "SELECT count(*) AS n FROM memory_version WHERE status = 'dormant'",
    )
    assert cur.fetchone()["n"] == 0


# --------------------------------------------------------------------------
# 18.1 session bundles
# --------------------------------------------------------------------------
@pytest.fixture
def session_id(cur):
    cur.execute("INSERT INTO agent_session (agent) VALUES ('claude') RETURNING session_id")
    return cur.fetchone()["session_id"]


def _propose_in_session(cur, scope_id, session_id, type_, title):
    return proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload=_create_payload(scope_id, type=str(type_), title=title),
        session_id=session_id,
    )["proposal"]


def test_a_bundle_reads_grounds_before_conclusions(cur, scope_id, session_id):
    _propose_in_session(cur, scope_id, session_id, MemoryType.DECISION, "so we split the runner")
    _propose_in_session(cur, scope_id, session_id, MemoryType.OBSERVATION, "exit code was 1")
    _propose_in_session(cur, scope_id, session_id, MemoryType.INTERPRETATION, "the body ran on")

    queue = proposals.session_queue(cur)
    assert len(queue) == 1
    bundle = queue[0]
    assert bundle["session_id"] == session_id
    assert bundle["count"] == 3
    assert [p["memory_type"] for p in bundle["proposals"]] == [
        MemoryType.OBSERVATION,
        MemoryType.INTERPRETATION,
        MemoryType.DECISION,
    ]


def test_proposals_without_a_session_still_appear(cur, scope_id, session_id):
    """Nothing may fall out of the queue for lacking provenance."""
    _propose_in_session(cur, scope_id, session_id, MemoryType.OBSERVATION, "in a session")
    _propose_create(cur, scope_id, title="no session at all")

    queue = proposals.session_queue(cur)
    assert {b["session_id"] for b in queue} == {session_id, None}


def test_approving_a_bundle_leaves_out_what_was_rejected(cur, scope_id, session_id):
    keep = _propose_in_session(cur, scope_id, session_id, MemoryType.OBSERVATION, "exit code 1")
    drop = _propose_in_session(cur, scope_id, session_id, MemoryType.DECISION, "split it")

    approved = proposals.approve_bundle(
        cur, session_id, reviewer="user", skip={drop["proposal_id"]}
    )
    assert [row["proposal_id"] for row in approved] == [keep["proposal_id"]]
    assert proposals.get(cur, drop["proposal_id"])["status"] == ProposalStatus.PENDING

    entity = store.get_entity(cur, keep["target_memory"])
    assert entity["active_version"] == keep["applied_version"]


def test_an_empty_bundle_is_not_an_error(cur):
    assert proposals.approve_bundle(cur, uuid.uuid4(), reviewer="user") == []


def test_within_one_rank_the_order_is_the_order_they_were_made_in(cur, scope_id, session_id):
    """The tiebreak has to be an answer, not whatever the scan returns.

    Every proposal in one transaction carries the same created_at, since now()
    is transaction start time. Sorting on it left the order inside a rank
    undefined, and the import of 27.1 writes a whole file in one transaction.
    """
    titles = ("the ramp overshot", "the pump cavitated", "the logger dropped a frame")
    for title in titles:
        _propose_in_session(cur, scope_id, session_id, MemoryType.FACT, title)

    bundle = proposals.session_queue(cur)[0]
    stamps = {row["created_at"] for row in bundle["proposals"]}
    assert len(stamps) == 1, "the premise: one transaction gives one timestamp to all of them"

    # The order itself is the weaker check, because a small freshly written
    # table tends to come back in insertion order whatever the query asks for.
    # What the degenerate key could not do is distinguish these rows at all.
    keys = [row["seq"] for row in bundle["proposals"]]
    assert len(set(keys)) == len(titles)
    assert keys == sorted(keys)
    assert [row["title"] for row in bundle["proposals"]] == list(titles)


def test_types_the_section_does_not_name_still_sort_by_grounds(cur, scope_id, session_id):
    """A bundle must never read a decision before the fact supporting it."""
    _propose_in_session(cur, scope_id, session_id, MemoryType.DECISION, "so we kept OPLS-AA")
    _propose_in_session(cur, scope_id, session_id, MemoryType.HYPOTHESIS, "GAFF may be soft")
    _propose_in_session(cur, scope_id, session_id, MemoryType.FACT, "GAFF ran ten kelvin low")
    _propose_in_session(cur, scope_id, session_id, MemoryType.TASK, "rerun the sweep")

    bundle = proposals.session_queue(cur)[0]
    assert [p["memory_type"] for p in bundle["proposals"]] == [
        MemoryType.FACT,
        MemoryType.HYPOTHESIS,
        MemoryType.DECISION,
        MemoryType.TASK,
    ]


def test_an_auto_committed_task_completion_actually_completes_it(cur, scope_id):
    """Section 17 puts a simple task completion on the auto line.

    The proposal used to be marked auto_committed while the version did not
    move, which is section 1's "a finished task is treated as unfinished"
    produced by the thing built to prevent it.
    """
    memory_id, version_id = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.TASK,
        title="rerun the sweep once the guard is in",
        content="carried over because the next session would proceed on the old numbers",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )

    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CHANGE_STATUS,
        payload={
            "version_id": str(version_id),
            "status": str(VersionStatus.COMPLETED),
            "reason": "the sweep finished and the numbers are in",
        },
        target_memory=memory_id,
    )

    assert result["proposal"]["status"] == "auto_committed"
    assert store.get_version(cur, version_id)["status"] == str(VersionStatus.COMPLETED)
    assert store.get_entity(cur, memory_id)["active_version"] == version_id


def test_a_status_change_that_waits_does_not_move_the_version(cur, scope_id):
    """Only the auto line writes at propose time; the rest waits for the review."""
    memory_id, version_id = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="the probe reports the enemy column",
        content="read as the friendly column for two sessions",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )

    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CHANGE_STATUS,
        payload={
            "version_id": str(version_id),
            "status": str(VersionStatus.DISPROVEN),
            "reason": "the column belongs to the other side",
        },
        target_memory=memory_id,
    )

    assert result["proposal"]["status"] == "pending"
    assert store.get_version(cur, version_id)["status"] == str(VersionStatus.CANDIDATE)


def test_a_rejected_idea_worded_differently_still_comes_back_with_its_reason(cur, scope_id):
    """Specification 15.1's stated purpose, which an exact title test could not serve.

    A rejection the proposer never learns about is one it walks into again,
    and an extraction running unattended words the same insight differently
    every night.
    """
    first = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.FACT),
            "title": "the enclosure bridge chip times out",
            "content": "the drive drops off the bus under load",
            "source_type": str(SourceType.AGENT),
        },
    )
    proposals.reject(
        cur,
        first["proposal"]["proposal_id"],
        reviewer="user",
        reason="the bridge chip is fine; the enclosure loses power",
    )

    with pytest.raises(DuplicateProposalError) as caught:
        proposals.propose(
            cur,
            actor="claude",
            operation=ProposalOperation.CREATE,
            payload={
                "scope_id": str(scope_id),
                "type": str(MemoryType.FACT),
                "title": "the enclosure bridge chip times out under load",
                "content": "a second run of the same reading",
                "source_type": str(SourceType.AGENT),
            },
            allow_similar=True,
        )

    turned_down = caught.value.rejected
    assert len(turned_down) == 1
    assert turned_down[0]["decision_reason"] == "the bridge chip is fine; the enclosure loses power"


def test_an_unrelated_title_in_the_same_scope_is_not_a_duplicate(cur, scope_id):
    proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.FACT),
            "title": "the enclosure bridge chip times out",
            "content": "the drive drops off the bus under load",
            "source_type": str(SourceType.AGENT),
        },
    )
    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.PREFERENCE),
            "title": "read the echo line before reading the numbers",
            "content": "the probe prints what it was given before it prints results",
            "source_type": str(SourceType.AGENT),
        },
        allow_similar=True,
    )
    assert result["proposal"]["status"] == "pending"


def test_a_retirement_approved_at_review_takes_the_pointer_off(cur, scope_id):
    """The pointer has to move, not just the status (10, 16.1, 30 段 B).

    Retiring a version and leaving the entity pointing at it produces exactly
    the failure section 1 names: the memory stays current truth while its own
    row says it was withdrawn. This is the path every unattended retirement
    takes, because the worker holds all of them for review, so a break here is
    silent and permanent — the retirement reports success and retires nothing.
    """
    seed = _propose_create(cur, scope_id)["proposal"]
    version_id = seed["applied_version"]
    proposals.approve(cur, seed["proposal_id"], reviewer="user")
    assert store.get_entity(cur, seed["target_memory"])["active_version"] == version_id

    filed = proposals.propose(
        cur,
        actor="mashu-worker",
        operation=ProposalOperation.CHANGE_STATUS,
        target_memory=seed["target_memory"],
        payload={
            # A string, as it will be after a round trip through JSONB. The
            # comparison inside set_status is against a UUID.
            "version_id": str(version_id),
            "status": str(VersionStatus.DORMANT),
            "reason": "the run it described was replaced",
        },
        allow_duplicate=True,
    )
    proposals.approve(cur, filed["proposal"]["proposal_id"], reviewer="user")

    assert store.get_version(cur, version_id)["status"] == VersionStatus.DORMANT
    assert store.get_entity(cur, seed["target_memory"])["active_version"] is None


def test_a_candidate_waiting_does_not_raise_the_review_warning(cur, scope_id):
    _propose_create(cur, scope_id)
    got = proposals.backlog(cur)
    assert (got["waiting"], got["blocking"], got["ok"]) == (1, 0, True)
    assert got["warning"] is None


def test_a_proposal_held_for_a_person_raises_it_at_once(cur, scope_id):
    _propose_create(cur, scope_id)
    _propose_create(cur, scope_id, allow_duplicate=True, allow_similar=True)
    got = proposals.backlog(cur)
    assert got["blocking"] == 1
    assert got["ok"] is False
    assert "nothing else will move them" in got["warning"]


def test_an_old_candidate_raises_it_even_with_nothing_held(cur, scope_id):
    made = _propose_create(cur, scope_id)["proposal"]
    cur.execute(
        "UPDATE proposal SET created_at = now() - make_interval(days => %s) WHERE proposal_id = %s",
        (proposals.REVIEW_STALE_DAYS + 2, made["proposal_id"]),
    )
    got = proposals.backlog(cur)
    assert got["blocking"] == 0
    assert got["ok"] is False
    assert "has waited" in got["warning"]


def test_deciding_takes_it_back_off_the_warning(cur, scope_id):
    made = _propose_create(cur, scope_id)
    proposals.approve(cur, made["proposal"]["proposal_id"], reviewer="user")
    got = proposals.backlog(cur)
    assert (got["waiting"], got["oldest_days"], got["ok"]) == (0, None, True)
