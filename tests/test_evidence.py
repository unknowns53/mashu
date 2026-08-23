"""Reference edges: what a memory rests on, and what rests on it (specification 19)."""

import pytest

from mashu import proposals, store
from mashu.errors import MashuError, NotFoundError
from mashu.models import (
    EventType,
    MemoryType,
    ProposalOperation,
    SourceType,
    VersionStatus,
)


def make(cur, scope_id, title, *, adopt=True, type=MemoryType.FACT, evidence=None):
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=type,
        title=title,
        content=f"content of {title}",
        source_type=SourceType.USER,
        created_by="tester",
        actor="tester",
        adopt=adopt,
        evidence=evidence,
    )


def test_an_edge_records_the_ground_and_reads_back_with_its_standing(cur, scope_id):
    ground, _ = make(cur, scope_id, "the measurement")
    _, conclusion_version = make(cur, scope_id, "what it means")

    written = store.record_evidence(
        cur, from_version=conclusion_version, to_memory=[ground], actor="tester"
    )
    assert written == 1

    rows = store.evidence_for(cur, conclusion_version)
    assert [r["memory_id"] for r in rows] == [ground]
    assert rows[0]["title"] == "the measurement"
    assert rows[0]["version_status"] == str(VersionStatus.CANDIDATE)


def test_the_same_edge_twice_is_not_two_edges(cur, scope_id):
    ground, _ = make(cur, scope_id, "the measurement")
    _, version = make(cur, scope_id, "what it means")

    store.record_evidence(cur, from_version=version, to_memory=[ground], actor="tester")
    again = store.record_evidence(cur, from_version=version, to_memory=[ground], actor="tester")

    assert again == 0
    assert len(store.evidence_for(cur, version)) == 1


def test_nothing_may_rest_on_itself(cur, scope_id):
    """Section 14 lets a summary say only what its references say.

    An entity that is its own reference makes that condition say nothing.
    """
    memory_id, version_id = make(cur, scope_id, "a memory")
    with pytest.raises(MashuError, match="its own entity"):
        store.record_evidence(cur, from_version=version_id, to_memory=[memory_id], actor="tester")


def test_a_ground_that_does_not_exist_is_refused(cur, scope_id):
    from uuid import uuid4

    _, version_id = make(cur, scope_id, "a memory")
    with pytest.raises(NotFoundError):
        store.record_evidence(cur, from_version=version_id, to_memory=[uuid4()], actor="tester")


def test_recording_an_edge_leaves_an_event(cur, scope_id):
    ground, _ = make(cur, scope_id, "the measurement")
    conclusion, version = make(cur, scope_id, "what it means")
    store.record_evidence(cur, from_version=version, to_memory=[ground], actor="tester")

    cur.execute(
        "SELECT * FROM event_log WHERE event_type = %s",
        (str(EventType.EVIDENCE_RECORDED),),
    )
    row = cur.fetchone()
    assert row["memory_id"] == conclusion
    assert row["detail"]["written"] == 1


# --------------------------------------------------------------------------
# the reverse lookup, which is the reason the table exists
# --------------------------------------------------------------------------
def test_the_reverse_lookup_names_what_was_built_on_a_memory(cur, scope_id):
    ground, _ = make(cur, scope_id, "the measurement")
    conclusion, version = make(cur, scope_id, "what it means")
    store.record_evidence(cur, from_version=version, to_memory=[ground], actor="tester")

    dependants = store.resting_on(cur, ground)
    assert [r["memory_id"] for r in dependants] == [conclusion]
    assert dependants[0]["is_active"] is True


def test_a_retired_dependant_is_still_listed_but_not_as_active(cur, scope_id):
    """A version that has been replaced is not a live concern, and has to be separable."""
    ground, _ = make(cur, scope_id, "the measurement")
    conclusion, first = make(cur, scope_id, "what it means")
    store.record_evidence(cur, from_version=first, to_memory=[ground], actor="tester")

    second = store.add_version(
        cur,
        memory_id=conclusion,
        content="a second reading",
        source_type=SourceType.USER,
        created_by="tester",
        actor="tester",
        based_on_version=first,
        adopt=True,
        evidence=[ground],
    )

    dependants = store.resting_on(cur, ground)
    assert {r["version_id"] for r in dependants} == {first, second}
    assert [r["is_active"] for r in dependants] == [True, False]
    assert next(r for r in dependants if r["version_id"] == second)["is_active"] is True


def test_disproving_a_ground_does_not_touch_what_rests_on_it(cur, scope_id):
    """Specification 19: the enumeration goes to review, nothing cascades.

    Whether a conclusion survives losing one of its grounds is a judgement, and
    the layer does not make judgements.
    """
    ground, ground_version = make(cur, scope_id, "the measurement")
    conclusion, version = make(cur, scope_id, "what it means")
    store.record_evidence(cur, from_version=version, to_memory=[ground], actor="tester")

    store.set_status(
        cur,
        version_id=ground_version,
        target=VersionStatus.DISPROVEN,
        actor="tester",
        reason="the probe was measuring the wrong column",
    )

    assert store.get_entity(cur, conclusion)["active_version"] == version
    assert store.resting_on(cur, ground)[0]["is_active"] is True
    assert store.evidence_for(cur, version)[0]["version_status"] is None


# --------------------------------------------------------------------------
# the write paths that carry references
# --------------------------------------------------------------------------
def test_an_entity_can_be_created_with_its_grounds(cur, scope_id):
    ground, _ = make(cur, scope_id, "the measurement")
    _, version = make(cur, scope_id, "a state", type=MemoryType.STATE, evidence=[ground])
    assert [r["memory_id"] for r in store.evidence_for(cur, version)] == [ground]


def test_a_proposal_carries_its_references_through_review(cur, scope_id):
    """The references have to survive the wait, not only the immediate write."""
    ground, _ = make(cur, scope_id, "the measurement")

    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.STATE),
            "title": "where the scope stands",
            "content": "a summary that says only what its references say",
            "source_type": str(SourceType.AGENT),
            "evidence": [str(ground)],
        },
    )
    proposal = result["proposal"]
    assert proposal["status"] == "pending"

    version_id = proposals.get(cur, proposal["proposal_id"])["applied_version"]
    assert [r["memory_id"] for r in store.evidence_for(cur, version_id)] == [ground]

    proposals.approve(
        cur, proposal["proposal_id"], reviewer="user", reason="the references check out"
    )
    assert [r["memory_id"] for r in store.evidence_for(cur, version_id)] == [ground]
    assert store.resting_on(cur, ground)[0]["is_active"] is True


def test_a_held_update_keeps_its_directive_and_references_until_approval(cur, scope_id):
    """The approval path used to build its version from scratch and drop both."""
    ground, _ = make(cur, scope_id, "the measurement")
    memory_id, first = make(cur, scope_id, "a preference", type=MemoryType.PREFERENCE)

    # Asking to switch the active version puts the change on the human review
    # line, which is the one path that writes nothing at propose time and has
    # to rebuild the version from the payload when the review comes back.
    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.UPDATE_VERSION,
        payload={
            "content": "the long form, with the reasoning",
            "directive": "the short standing form",
            "source_type": str(SourceType.AGENT),
            "evidence": [str(ground)],
            "switches_active": True,
        },
        target_memory=memory_id,
        based_on_version=first,
    )
    proposal = result["proposal"]
    assert proposals.get(cur, proposal["proposal_id"])["applied_version"] is None

    proposals.approve(cur, proposal["proposal_id"], reviewer="user", reason="fine")

    active = store.get_entity(cur, memory_id)["active_version"]
    assert store.get_version(cur, active)["directive"] == "the short standing form"
    assert [r["memory_id"] for r in store.evidence_for(cur, active)] == [ground]


def test_merging_moves_the_edges_that_point_at_the_source(cur, scope_id):
    """20.2 step 5, now reachable because something finally writes the edges."""
    source, _ = make(cur, scope_id, "SSD failure analysis")
    target, _ = make(cur, scope_id, "SSD debugging")
    _, citing = make(cur, scope_id, "what we concluded")
    store.record_evidence(cur, from_version=citing, to_memory=[source], actor="tester")

    surviving = store.get_entity(cur, target)["active_version"]
    store.merge_entities(
        cur,
        source=source,
        target=target,
        actor="user",
        reason="the same investigation",
        keep_active=surviving,
    )

    assert store.resting_on(cur, source) == []
    assert [r["memory_id"] for r in store.resting_on(cur, target)] == [
        store.get_version(cur, citing)["memory_id"]
    ]
    assert [r["memory_id"] for r in store.evidence_for(cur, citing)] == [target]
