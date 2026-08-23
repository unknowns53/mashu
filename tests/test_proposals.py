"""Proposals, the three commit lines, and the review queue.

Specifications 15, 15.1, 17 and 18.1.
"""

from __future__ import annotations

import uuid

import pytest

from mashu import proposals, store
from mashu.errors import DuplicatePendingError, ProposalError
from mashu.gate import CommitDecision
from mashu.models import (
    EntityStatus,
    MemoryType,
    ProposalOperation,
    ProposalStatus,
    SourceType,
    VersionStatus,
)


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


def _propose_create(cur, scope_id, **over):
    return proposals.propose(
        cur,
        actor="agent-1",
        operation=ProposalOperation.CREATE,
        payload=_create_payload(scope_id, **over),
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


def test_rejecting_makes_the_candidate_dormant_rather_than_deleting_it(cur, scope_id):
    """Deleting it would let the same proposal come straight back."""
    result = _propose_create(cur, scope_id)
    pid = result["proposal"]["proposal_id"]

    decided = proposals.reject(cur, pid, reviewer="user", reason="this was the old harness")
    assert decided["status"] == ProposalStatus.REJECTED
    assert decided["decision_reason"]

    version = store.get_version(cur, decided["applied_version"])
    assert version["status"] == VersionStatus.DORMANT
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
def test_preference_is_applied_without_review(cur, scope_id):
    result = _propose_create(
        cur, scope_id, type=str(MemoryType.PREFERENCE), title="commit messages in English"
    )
    assert result["ruling"].decision is CommitDecision.AUTO

    proposal = result["proposal"]
    assert proposal["status"] == ProposalStatus.AUTO_COMMITTED
    assert proposal["decided_at"] is not None
    assert proposal["reviewer"] is None

    entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] == proposal["applied_version"]


def test_auto_committed_work_does_not_enter_the_queue(cur, scope_id):
    _propose_create(cur, scope_id, type=str(MemoryType.PREFERENCE), title="a preference")
    assert proposals.session_queue(cur) == []


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

    with pytest.raises(DuplicatePendingError) as caught:
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
    with pytest.raises(DuplicatePendingError):
        _propose_create(cur, scope_id)


def test_a_different_title_is_not_a_duplicate(cur, scope_id):
    _propose_create(cur, scope_id)
    result = _propose_create(cur, scope_id, title="something else entirely")
    assert result["proposal"]["status"] == ProposalStatus.PENDING


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
