"""Scenario 3: a near-duplicate entity must not be created silently.

    Three titles that mean the same thing arrive over time. The third one must
    not become a third entity without the agent having been shown the first
    two, and choosing.

Specification 20 forbids fully automatic creation. The similarity threshold is
deliberately not fixed in the specification; section 27.2 sets it from measured
score distributions once the embedding model is in place.

What the scenario exposed
-------------------------
The specification says an agent that creates a new entity despite a high
similarity score goes to human review, but it does not say what the entity does
in the meantime. Either it exists provisionally and retrieval can already find
two entities for one concept, or it does not exist until the review clears,
and the agent cannot attach a version to it. The Commit Gate in section 17
lists Entity Merge under human review but says nothing about creation, so this
has to be settled before Entity Resolution is built.
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
