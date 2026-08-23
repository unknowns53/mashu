"""Scenario 2: a superseded version must not reach the agent.

    A memory is corrected. The old wording stays in history for the record,
    but only the current wording may be assembled into context.

Specification 21 does this structurally through the Active Version Filter rather
than by weighting recency, which is why the scenario checks the pointer and the
status rather than an ordering.
"""

from mashu import retrieval, store
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


def test_context_assembly_carries_only_the_current_wording(cur, scope_id, author):
    """The superseded wording must not appear in any of the three layers.

    Layer 3 is checked as well as layer 1. Superseded content is deliberately
    not retired knowledge: the entity has a current answer, and showing the old
    wording alongside it would put two readings of one memory in front of the
    agent with nothing to choose between them.
    """
    memory_id, first = author("the ramp rate", "the cloud point ramp is one degree per minute")
    store.add_version(
        cur,
        memory_id=memory_id,
        content="the cloud point ramp is half a degree per minute",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=True,
    )

    got = retrieval.retrieve(cur, "the ramp rate", actor="claude", scope_id=scope_id)
    handed_over = " ".join(
        row.get("content", "") + row.get("reason", "") or ""
        for row in (*got.active, *got.unreviewed, *got.retired)
    )
    assert "one degree per minute" not in handed_over
    assert "half a degree per minute" in handed_over
    assert [row["memory_id"] for row in got.retired] == []
