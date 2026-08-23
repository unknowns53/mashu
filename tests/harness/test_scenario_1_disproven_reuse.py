"""Scenario 1: a disproven hypothesis must not come back.

    Day 1   The agent records a hypothesis about why the drive times out and
            it is adopted as the current reading.
    Day 3   A measurement contradicts it and the user marks it disproven.
    Day 10  The same question is asked again.

    The disproven content must not reach the agent as current knowledge.

This is the first problem named in specification 1 and the second success
criterion in specification 31.

What the scenario exposed, and how v0.4 answered it
---------------------------------------------------
Filtering the hypothesis out is not the same as preventing its reuse. Once the
entity has no active version it became invisible, so on day 10 the agent was
free to derive the very same hypothesis from scratch and learn nothing from the
earlier refutation.

Section 21.1 now returns a third layer. A disproven version reaches the agent
as its title, its status and the reason it was refuted, with the content held
back so that nothing retired can be read as current. The schema backs this up:
a disproven version without a reason is refused, because layer 3 would have
nothing to hand over.
"""

from mashu import retrieval, store
from mashu.models import MemoryType, VersionStatus


def test_day_3_disproving_removes_the_current_reading(cur, author):
    memory_id, version_id = author(
        "SSD timeout cause",
        "the timeout comes from the enclosure bridge chip",
        type=MemoryType.HYPOTHESIS,
    )
    assert store.get_entity(cur, memory_id)["active_version"] == version_id

    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="the timeout reproduced over a direct SATA connection",
    )

    assert store.get_entity(cur, memory_id)["active_version"] is None
    assert store.get_version(cur, version_id)["status"] == VersionStatus.DISPROVEN


def test_the_refutation_and_its_reason_stay_readable(cur, author):
    """Specification 31: the history and the motive have to remain traceable."""
    memory_id, version_id = author(
        "SSD timeout cause",
        "the timeout comes from the enclosure bridge chip",
        type=MemoryType.HYPOTHESIS,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="the timeout reproduced over a direct SATA connection",
    )

    cur.execute(
        "SELECT detail FROM event_log WHERE memory_id = %s AND event_type = %s",
        (memory_id, "status_changed"),
    )
    reasons = [row["detail"]["reason"] for row in cur.fetchall()]
    assert "the timeout reproduced over a direct SATA connection" in reasons


def test_day_10_the_same_question_does_not_return_the_disproven_hypothesis(cur, scope_id, author):
    """The refutation comes back; the refuted claim does not.

    Absence alone would not close the scenario. An agent told nothing about the
    bridge chip is free to propose the bridge chip again, so layer 3 hands over
    the reason instead of the content: what stops the rederivation is the
    measurement that killed it, not the silence.
    """
    memory_id, version_id = author(
        "SSD timeout cause",
        "the timeout comes from the enclosure bridge chip",
        type=MemoryType.HYPOTHESIS,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="the timeout reproduced over a direct SATA connection",
    )

    got = retrieval.retrieve(cur, "why does the SSD time out", actor="claude", scope_id=scope_id)

    assert all("bridge chip" not in row["content"] for row in got.active)
    assert all("bridge chip" not in row["content"] for row in got.unreviewed)

    retired = {row["memory_id"]: row for row in got.retired}
    assert memory_id in retired
    assert retired[memory_id]["status"] == VersionStatus.DISPROVEN
    assert "direct SATA connection" in retired[memory_id]["reason"]
    assert "content" not in retired[memory_id]
