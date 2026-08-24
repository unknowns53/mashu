"""What the running system is costing, and what it cannot yet say (27.3)."""

from __future__ import annotations

import uuid

from mashu import metrics, proposals, store
from mashu.models import Delivery, MemoryType, ProposalOperation, SourceType


def _seed(cur, scope_id, *, adopt):
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.PREFERENCE,
        title=f"規律 {uuid.uuid4()}",
        content="短く。",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=adopt,
    )


def test_the_unreviewed_share_is_taken_from_the_store_not_from_a_search(cur, scope_id):
    """27.3 measures the population. A share read off the returned set would be
    measuring whatever cap the retrieval applied, not whether review is keeping
    up with what has accumulated."""
    for _ in range(3):
        _seed(cur, scope_id, adopt=True)
    _seed(cur, scope_id, adopt=False)

    got = metrics.collect(cur)
    row = next(r for r in got.unreviewed if r["held"] == 4)
    assert row["adopted"] == 3
    assert row["unreviewed"] == 1
    assert row["share"] == 0.25


def test_an_indicator_with_no_source_is_named_rather_than_left_out(cur, scope_id):
    """A page showing only the answerable figures reads as a full account of a
    system half of whose failure modes nothing is watching."""
    got = metrics.collect(cur)
    assert got.unmeasured
    assert any("段 D" in line for line in got.unmeasured)
    assert any("not timed" in line for line in got.unmeasured)


def test_the_opening_cost_is_reported_per_scope_against_the_ceiling(cur, scope_id):
    """The fixed cost every session pays before it asks anything (21.2)."""
    memory_id, _ = _seed(cur, scope_id, adopt=True)
    store.set_delivery(cur, memory_id=memory_id, delivery=Delivery.STARTUP_REQUIRED, actor="user")

    got = metrics.collect(cur)
    row = next(r for r in got.openings if r["startup"] >= 1)
    assert row["cost"] <= row["budget"]
    assert row["trimmed"] == 0


def test_a_rejection_counts_as_a_correction(cur, scope_id):
    """27.3 counts what a person had to take back, and sets no threshold on it."""
    before = metrics.collect(cur).corrections["rejected"]
    made = proposals.propose(
        cur,
        actor="agent-1",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.OBSERVATION),
            "title": f"見たこと {uuid.uuid4()}",
            "content": "本文",
            "source_type": str(SourceType.AGENT),
        },
    )
    proposals.reject(cur, made["proposal"]["proposal_id"], reviewer="user", reason="要らない")

    got = metrics.collect(cur)
    assert got.corrections["rejected"] == before + 1
    assert got.corrections["per_100_retrievals"] is None or got.corrections["retrievals"] >= 0
