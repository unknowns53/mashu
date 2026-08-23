"""Scenario 5: what retrieval does while a candidate waits for review.

    An agent proposes something on Monday. Nobody reviews it. On Friday the
    agent asks a question that the pending content would have answered.

Specification 21 assembles context from active version content only, so a
pending candidate contributes nothing. The scenario pins that down because the
consequence is easy to miss.

What the scenario exposed, and how v0.4 answered it
---------------------------------------------------
Two behaviours followed that the specification never stated outright.

First, an entity whose first version is still pending was invisible in its
entirety. Neither the content nor the fact that a proposal existed reached
retrieval, so the agent could propose the same thing again on Friday and the
queue grew duplicates of one unreviewed idea. Section 21.1 answered with a
second layer, and section 15.1 with a duplicate check at propose time.

Second, review latency turned out to be a correctness matter rather than a
matter of tidiness, so section 27.3 began recording how long each proposal
waits and not only how many minutes a day the reviewing costs.

What changed again in v0.7
--------------------------
Layer 2 was defined as carrying the fact of a pending proposal without its
content, which made the waiting time an outage: Friday's question went
unanswered for as long as nobody reviewed. Measuring the review load showed
that outage arriving before the system was even in use.

So layer 2 now carries the content, tagged unreviewed. Review stopped being the
gate that makes knowledge usable and became the gate that confirms its quality.
The stall figure still matters, but it now measures how long something was
relied on unconfirmed rather than how long it was missing.

The assertion below therefore reads the opposite way round from the v0.4 note
above, which is the point of keeping both: the scenario is what noticed.
"""

from mashu import retrieval, store
from mashu.models import SourceType, VersionStatus


def test_a_first_version_awaiting_review_leaves_the_entity_without_a_truth(cur, author):
    memory_id, version_id = author(
        "DES water content",
        "the mixture picks up water above 40 percent humidity",
        adopt=False,
    )
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] is None
    assert entity["latest_version"] == version_id
    assert store.get_version(cur, version_id)["status"] == VersionStatus.CANDIDATE


def test_a_pending_candidate_does_not_disturb_the_version_in_place(cur, author):
    """A backlog on an existing entity leaves the current reading answering."""
    memory_id, first = author("DES water content", "the mixture is hygroscopic")
    pending_version = store.add_version(
        cur,
        memory_id=memory_id,
        content="the mixture picks up water above 40 percent humidity",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=False,
    )
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] == first
    assert entity["latest_version"] == pending_version


def test_approval_after_a_delay_still_works(cur, author):
    """Waiting does not expire a candidate; only a newer version supersedes it."""
    memory_id, first = author("DES water content", "the mixture is hygroscopic")
    pending_version = store.add_version(
        cur,
        memory_id=memory_id,
        content="the mixture picks up water above 40 percent humidity",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=False,
    )
    store.set_active(
        cur,
        memory_id=memory_id,
        version_id=pending_version,
        actor="user",
        reason="reviewed on Friday",
    )
    assert store.get_entity(cur, memory_id)["active_version"] == pending_version


def test_friday_gets_the_pending_content_tagged_rather_than_nothing(cur, scope_id, author):
    """v0.7: the candidate answers the question, and says it is unconfirmed."""
    memory_id, first = author("DES water content", "the mixture is hygroscopic")
    store.add_version(
        cur,
        memory_id=memory_id,
        content="the mixture picks up water above 40 percent humidity",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=False,
    )

    got = retrieval.retrieve(cur, "DES water content", actor="claude", scope_id=scope_id)

    assert [row["content"] for row in got.active] == ["the mixture is hygroscopic"]
    assert [row["content"] for row in got.unreviewed] == [
        "the mixture picks up water above 40 percent humidity"
    ]
    assert got.unreviewed[0]["tag"] == retrieval.UNREVIEWED_TAG
    assert got.unreviewed[0]["days_pending"] == 0


def test_the_unreviewed_layer_cannot_outgrow_the_reviewed_one(cur, scope_id, author):
    """The backlog is bounded in the context even when it is not bounded in the queue.

    A queue nobody works through would otherwise fill the context with tagged
    material, and a tag that is on everything tells the agent nothing.
    """
    memory_id, first = author("DES water content", "the mixture is hygroscopic")
    based_on = first
    for i in range(4):
        based_on = store.add_version(
            cur,
            memory_id=memory_id,
            content=f"a further unreviewed reading of the water content, run {i}",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            based_on_version=based_on,
            adopt=False,
        )

    got = retrieval.retrieve(cur, "DES water content", actor="claude", scope_id=scope_id)
    assert len(got.unreviewed) <= len(got.active)
    assert got.dropped_unreviewed == 4 - len(got.unreviewed)
