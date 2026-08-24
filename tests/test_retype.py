"""Correcting what kind of thing an entity is (specification 8, v0.11)."""

import pytest

from mashu import gate, proposals, store
from mashu.errors import MashuError, MergeError
from mashu.gate import CommitDecision
from mashu.models import (
    EntityStatus,
    EventType,
    MemoryType,
    ProposalOperation,
    SourceType,
)


@pytest.fixture
def entity(cur, scope_id):
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="the ordering was settled by the user",
        content="carrier scenario one first, then two",
        source_type=SourceType.FILE,
        created_by="import",
        actor="import",
        adopt=True,
    )


def test_the_type_moves_and_the_content_does_not(cur, entity):
    memory_id, version_id = entity

    moved = store.set_type(
        cur,
        memory_id=memory_id,
        target=MemoryType.DECISION,
        actor="user",
        reason="the body says the user decided it",
    )

    assert moved["from"] is MemoryType.FACT
    assert moved["to"] is MemoryType.DECISION
    after = store.get_entity(cur, memory_id)
    assert after["type"] == str(MemoryType.DECISION)
    assert after["active_version"] == version_id
    assert after["latest_version"] == version_id


def test_no_version_is_created(cur, entity):
    """A correction is not a change of mind about the content."""
    memory_id, _ = entity
    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    before = cur.fetchone()["n"]

    store.set_type(
        cur, memory_id=memory_id, target=MemoryType.DECISION, actor="user", reason="it is one"
    )

    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    assert cur.fetchone()["n"] == before


def test_the_correction_and_its_reason_are_logged(cur, entity):
    memory_id, _ = entity
    store.set_type(
        cur,
        memory_id=memory_id,
        target=MemoryType.DECISION,
        actor="user",
        reason="the body says the user decided it",
    )

    cur.execute("SELECT * FROM event_log WHERE event_type = %s", (str(EventType.TYPE_CORRECTED),))
    row = cur.fetchone()
    assert row["detail"] == {
        "from": "fact",
        "to": "decision",
        "reason": "the body says the user decided it",
    }


def test_delivery_does_not_follow_the_type(cur, entity):
    """Joining the session-start pack is the operation that passes admission control."""
    memory_id, _ = entity
    before = store.get_entity(cur, memory_id)["delivery"]

    store.set_type(
        cur, memory_id=memory_id, target=MemoryType.STATE, actor="user", reason="it is the state"
    )

    assert store.get_entity(cur, memory_id)["delivery"] == before


def test_a_type_cannot_move_to_itself(cur, entity):
    memory_id, _ = entity
    with pytest.raises(MashuError, match="already"):
        store.set_type(
            cur, memory_id=memory_id, target=MemoryType.FACT, actor="user", reason="no change"
        )


def test_a_merged_entity_is_not_retyped(cur, scope_id, entity):
    """It is no longer a thing with a type of its own."""
    memory_id, _ = entity
    target, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="the same ordering under another name",
        content="carrier scenario one first",
        source_type=SourceType.FILE,
        created_by="import",
        actor="import",
        adopt=False,
    )
    keep = store.get_entity(cur, memory_id)["active_version"]
    store.merge_entities(
        cur, source=memory_id, target=target, actor="user", reason="one concept", keep_active=keep
    )

    with pytest.raises(MergeError, match="was merged into"):
        store.set_type(
            cur, memory_id=memory_id, target=MemoryType.DECISION, actor="user", reason="too late"
        )


# --------------------------------------------------------------------------
# an agent may ask, and waits
# --------------------------------------------------------------------------
def test_an_agent_asking_to_retype_waits_for_a_person(cur, entity):
    """The type decides which line the entity's later changes take (17)."""
    memory_id, _ = entity
    ruling = gate.classify(operation=ProposalOperation.RETYPE, source_type=SourceType.AGENT)
    assert ruling.decision is CommitDecision.HUMAN_REVIEW

    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.RETYPE,
        payload={"type": str(MemoryType.DECISION), "reason": "the body says the user decided it"},
        target_memory=memory_id,
    )
    assert result["proposal"]["status"] == "pending"
    assert store.get_entity(cur, memory_id)["type"] == str(MemoryType.FACT)

    proposals.approve(
        cur, result["proposal"]["proposal_id"], reviewer="user", reason="agreed, it is a decision"
    )
    assert store.get_entity(cur, memory_id)["type"] == str(MemoryType.DECISION)


def test_a_user_stated_retype_still_waits(cur, entity):
    """Unlike every other type of change, saying it was the user does not shortcut this."""
    ruling = gate.classify(operation=ProposalOperation.RETYPE, source_type=SourceType.USER)
    assert ruling.decision is CommitDecision.HUMAN_REVIEW


def test_the_entity_status_is_untouched(cur, scope_id):
    """Retyping is not the review that settles a provisional entity."""
    memory_id, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="a rival reading",
        content="the body",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=False,
        entity_status=EntityStatus.PROVISIONAL,
    )
    store.set_type(
        cur, memory_id=memory_id, target=MemoryType.OBSERVATION, actor="user", reason="it is one"
    )
    assert store.get_entity(cur, memory_id)["status"] == str(EntityStatus.PROVISIONAL)


def test_a_title_can_be_corrected_without_making_a_version(cur, scope_id):
    """8, 14: the one type whose content is meant to move had a frozen name.

    A Current State is rewritten as the work advances. Its title stayed at
    whatever the first version said, so a memory whose body was up to date went
    on announcing the state it had left — and in the session opening, where the
    title is the line above the text that is right.
    """
    memory_id, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.STATE,
        title="shipped as far as the coral sea, next is the south pacific",
        content="the two layers are in; what is left is calibration",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )
    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    before = cur.fetchone()["n"]

    store.set_title(
        cur,
        memory_id=memory_id,
        title="shipped as far as the south pacific, calibration is what is left",
        actor="user",
        reason="the south pacific shipped",
    )

    entity = store.get_entity(cur, memory_id)
    assert entity["title"].startswith("shipped as far as the south pacific")
    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    assert cur.fetchone()["n"] == before, "a rename is not a change of content"


def test_the_title_embedding_moves_with_the_title(cur, scope_id):
    """20, 21.1: resolution compares against the title and layer 3 ranks by it.

    Leaving the old vector behind would keep the entity findable only under the
    name it no longer carries, which is the failure being ended rather than
    moved.
    """
    memory_id, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="the ramp is half a degree per minute",
        content="measured on the second run",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )
    cur.execute(
        "SELECT title_embedding::text AS vec FROM memory_entity WHERE memory_id = %s",
        (memory_id,),
    )
    before = cur.fetchone()["vec"]

    store.set_title(
        cur,
        memory_id=memory_id,
        title="the timeout is nine seconds",
        actor="user",
        reason="it was never about the ramp",
    )
    cur.execute(
        "SELECT title_embedding::text AS vec FROM memory_entity WHERE memory_id = %s",
        (memory_id,),
    )
    assert cur.fetchone()["vec"] != before


def test_a_retitle_proposal_waits_for_a_person(cur, scope_id):
    """An agent that could rename could walk an entity away from its duplicate."""
    memory_id, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="the ramp is half a degree per minute",
        content="measured on the second run",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )
    made = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.RETITLE,
        target_memory=memory_id,
        payload={"title": "something else entirely", "reason": "tidier"},
    )
    assert made["ruling"].decision is CommitDecision.HUMAN_REVIEW
    assert store.get_entity(cur, memory_id)["title"].startswith("the ramp")
