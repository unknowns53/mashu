"""Scenario 2: a superseded version must not reach the agent.

    A memory is corrected. The old wording stays in history for the record,
    but only the current wording may be assembled into context.

Specification 21 does this structurally through the Active Version Filter rather
than by weighting recency, which is why the scenario checks the pointer and the
status rather than an ordering.
"""

import pytest

from harness_marks import skip_until_retrieval
from mashu import store
from mashu.models import SourceType, VersionStatus


def test_correcting_a_memory_retires_the_old_wording(cur, author):
    memory_id, first = author(
        "python environment",
        "the analysis runs on Python 3.11",
    )
    second = store.add_version(
        cur,
        memory_id=memory_id,
        content="the analysis runs on Python 3.13",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        based_on_version=first,
        adopt=True,
        reason="upgraded the interpreter",
    )

    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] == second
    assert store.get_version(cur, first)["status"] == VersionStatus.SUPERSEDED
    assert store.get_version(cur, second)["supersedes"] == first


def test_the_old_wording_survives_in_history(cur, author):
    """History is immutable: the correction adds, it does not overwrite."""
    memory_id, first = author("python environment", "the analysis runs on Python 3.11")
    store.add_version(
        cur,
        memory_id=memory_id,
        content="the analysis runs on Python 3.13",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        based_on_version=first,
        adopt=True,
    )
    assert store.get_version(cur, first)["content"] == "the analysis runs on Python 3.11"


@pytest.mark.pending_phase("retrieval")
@skip_until_retrieval
def test_context_assembly_carries_only_the_current_wording():
    """Query the corrected memory and assert the superseded text is absent."""
