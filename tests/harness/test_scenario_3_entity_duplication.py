"""Scenario 3: a near-duplicate entity must not be created silently.

    Three titles that mean the same thing arrive over time. The third one must
    not become a third entity without the agent having been shown the first
    two, and choosing.

Specification 20 forbids fully automatic creation. The similarity threshold is
deliberately not fixed in the specification; section 27.2 sets it from measured
score distributions once the embedding model is in place.

What the scenario exposed, and how v0.4 answered it
---------------------------------------------------
The specification sent an agent that creates a new entity despite a high
similarity score to human review, but said nothing about what the entity does
in the meantime. Either it existed and retrieval could find two entities for
one concept, or it did not exist and the agent could not attach a version.

Section 20.1 gives the entity its own status. It is created as provisional, so
the agent keeps working, and it stays out of layer 1 of retrieval, so one
concept never has two entities answering as current. Section 17 now lists
entity creation above the threshold under human review, and section 20.2 sets
out the merge that follows when the review calls it a duplicate.
"""

import pytest

from harness_marks import skip_until_entity_resolution
from mashu.models import MemoryType

NEAR_DUPLICATE_TITLES = [
    "SSD failure analysis",
    "SSD debugging",
    "external SSD problem",
]


def test_the_titles_the_threshold_has_to_separate(cur, scope_id, author):
    """Register the confusable set that section 27.2 measures against.

    Nothing here asserts a threshold. It records the fixture the measurement
    will use, so the three titles live in one place instead of in a notebook.
    """
    created = [
        author(title, f"notes about {title}", type=MemoryType.OBSERVATION)
        for title in NEAR_DUPLICATE_TITLES
    ]
    assert len({memory_id for memory_id, _ in created}) == 3


@pytest.mark.pending_phase("entity_resolution")
@skip_until_entity_resolution
def test_a_confusable_title_offers_the_existing_entities_first():
    """Propose the third title and assert the first two come back as candidates."""


@pytest.mark.pending_phase("entity_resolution")
@skip_until_entity_resolution
def test_creating_anyway_above_the_threshold_goes_to_review():
    """Choose new entity despite a high score and assert a review item appears."""


def test_review_calling_it_a_duplicate_folds_the_entity_back_in(cur, scope_id, author):
    """The ending of the scenario once the review decides (specification 20.2)."""
    from mashu import store
    from mashu.models import EntityStatus, MemoryType, SourceType, VersionStatus

    established, established_v = author(
        "SSD failure analysis", "the enclosure bridge chip times out"
    )
    provisional, provisional_v = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.OBSERVATION,
        title="SSD debugging",
        content="the drive times out over the enclosure",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
        entity_status=EntityStatus.PROVISIONAL,
    )

    store.merge_entities(
        cur,
        source=provisional,
        target=established,
        actor="user",
        reason="the same investigation under two names",
        keep_active=established_v,
    )

    assert store.get_entity(cur, provisional)["status"] == EntityStatus.MERGED
    assert store.get_entity(cur, established)["active_version"] == established_v
    assert store.get_version(cur, provisional_v)["memory_id"] == established
    assert store.get_version(cur, provisional_v)["status"] == VersionStatus.SUPERSEDED
