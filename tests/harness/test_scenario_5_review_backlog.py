"""Scenario 5: what retrieval does while a candidate waits for review.

    An agent proposes something on Monday. Nobody reviews it. On Friday the
    agent asks a question that the pending content would have answered.

Specification 21 assembles context from active version content only, so a
pending candidate contributes nothing. The scenario pins that down because the
consequence is easy to miss.

What the scenario exposed
-------------------------
Two behaviours follow that the specification never states outright.

First, an entity whose very first version is still pending is invisible in its
entirety. There is no active version to filter down to, so neither the content
nor the fact that a proposal exists reaches retrieval. The agent can therefore
propose the same thing again on Friday, and the review queue grows duplicates
of a single unreviewed idea.

Second, this makes review latency a correctness matter rather than a matter of
tidiness. Section 27.3 measures the review load in minutes per day; the number
that actually matters here is how long a candidate waits, because that is how
long the knowledge is unavailable. Worth deciding before the Review CLI is
built whether a pending candidate should be visible to its own proposer.
"""

import pytest

from harness_marks import skip_until_retrieval
from mashu import store
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


@pytest.mark.pending_phase("retrieval")
@skip_until_retrieval
def test_a_pending_candidate_is_absent_from_assembled_context():
    """Query the pending content and assert it does not appear."""
