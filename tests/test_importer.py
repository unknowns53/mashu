"""Bringing the existing store across (specification 27.1)."""

from __future__ import annotations

from mashu import importer, proposals, retrieval, store
from mashu.models import EntityStatus, MemoryType, ProposalStatus, SourceType


def _item(title, content, type=MemoryType.OBSERVATION, **over):
    item = {
        "type": str(type),
        "title": title,
        "content": content,
        "source_reference": "memory/ssd-investigation.md",
    }
    item.update(over)
    return item


def test_imported_items_arrive_as_candidates(cur, scope_id):
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("SSD failure analysis", "the enclosure bridge chip times out")],
        actor="import",
    )
    assert len(summary["created"]) == 1

    proposal = proposals.get(cur, summary["created"][0]["proposal_id"])
    assert proposal["status"] == ProposalStatus.PENDING

    entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] is None


def test_a_preference_is_held_too(cur, scope_id):
    """27.1 overrides the auto line: nothing arrives already trusted."""
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("commit messages", "keep them in English", type=MemoryType.PREFERENCE)],
        actor="import",
    )
    proposal = proposals.get(cur, summary["created"][0]["proposal_id"])
    assert proposal["status"] == ProposalStatus.PENDING
    assert "27.1" in proposal["decision_reason"]
    assert store.get_entity(cur, proposal["target_memory"])["active_version"] is None


def test_a_user_sourced_item_is_held_too(cur, scope_id):
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[
            _item(
                "the ramp rate",
                "half a degree per minute",
                type=MemoryType.FACT,
                source_type=str(SourceType.USER),
            )
        ],
        actor="import",
    )
    proposal = proposals.get(cur, summary["created"][0]["proposal_id"])
    assert proposal["status"] == ProposalStatus.PENDING


def test_the_origin_is_recorded_on_the_version(cur, scope_id):
    """Without it an imported claim looks like one the system observed."""
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("SSD failure analysis", "the bridge chip times out")],
        actor="import",
    )
    proposal = proposals.get(cur, summary["created"][0]["proposal_id"])
    version = store.get_version(cur, proposal["applied_version"])
    assert version["source_reference"] == "memory/ssd-investigation.md"
    assert version["source_type"] == SourceType.FILE


def test_an_item_without_an_origin_is_refused_and_reported(cur, scope_id):
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[
            _item("has an origin", "fine"),
            {"type": "fact", "title": "no origin", "content": "where did this come from"},
        ],
        actor="import",
    )
    assert len(summary["created"]) == 1
    assert summary["failed"] == [
        {"index": 1, "title": "no origin", "missing": ["source_reference"]}
    ]


def test_a_repeated_subject_becomes_a_version_not_a_rival(cur, scope_id):
    """The same subject across several files is one entity, not several."""
    importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("SSD failure analysis", "the bridge chip times out")],
        actor="import",
    )
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[
            _item(
                "SSD failure analysis",
                "it also times out over a direct connection",
                source_reference="memory/ssd-followup.md",
            )
        ],
        actor="import",
    )
    assert summary["created"] == []
    assert len(summary["attached"]) == 1
    assert summary["attached"][0]["onto"] == "SSD failure analysis"


def test_an_unrelated_subject_gets_its_own_entity(cur, scope_id):
    importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("SSD failure analysis", "the bridge chip times out")],
        actor="import",
    )
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("cloud point ramp rate", "half a degree per minute")],
        actor="import",
    )
    assert len(summary["created"]) == 1
    entity = store.get_entity(
        cur, proposals.get(cur, summary["created"][0]["proposal_id"])["target_memory"]
    )
    assert entity["status"] == EntityStatus.ACTIVE


def test_imported_content_is_not_readable_until_something_is_reviewed(cur, scope_id):
    """Semi-approval does not reach a scope whose every memory is imported.

    The name of this test used to say the opposite, which is where the drift
    shows. v0.7 made layer 2 hand over content so that waiting for review was
    no longer an outage; v0.8 then capped layer 2 at the size of layer 1, and
    a freshly imported scope has no layer 1 at all. So the migration lands in
    the one state semi-approval does not cover, and the whole import is
    invisible until a first review gives the scope something active.

    Pinned as it stands rather than as it ought to be. Which of the two rules
    yields is a decision about 21.1, not something to settle by editing an
    assertion.
    """
    importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("SSD failure analysis", "the enclosure bridge chip times out")],
        actor="import",
    )
    got = retrieval.retrieve(cur, "SSD failure analysis", actor="claude", scope_id=scope_id)
    assert got.active == []
    assert got.unreviewed == []  # nothing active to contrast the tag against


# --------------------------------------------------------------------------
# stocktaking
# --------------------------------------------------------------------------
def _author(cur, scope_id, type, title, content, adopt=True):
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=type,
        title=title,
        content=content,
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=adopt,
    )


def test_a_scope_counts_as_migrated_once_the_three_stand_up(cur, scope_id):
    rows = {r["scope_id"]: r for r in importer.stocktake(cur)}
    assert rows[scope_id]["migrated"] is False

    _author(cur, scope_id, MemoryType.STATE, "where the work stands", "phase 2 is in")
    _author(cur, scope_id, MemoryType.PREFERENCE, "commit language", "English")
    _author(cur, scope_id, MemoryType.DECISION, "storage", "PostgreSQL with pgvector")

    rows = {r["scope_id"]: r for r in importer.stocktake(cur)}
    assert rows[scope_id]["migrated"] is True
    assert rows[scope_id]["state"] == 1


def test_a_queued_current_state_does_not_count_as_rebuilt(cur, scope_id):
    """Queued is not the same as rebuilt, and the difference is the point."""
    _author(cur, scope_id, MemoryType.STATE, "where the work stands", "phase 2", adopt=False)
    _author(cur, scope_id, MemoryType.PREFERENCE, "commit language", "English")
    _author(cur, scope_id, MemoryType.DECISION, "storage", "PostgreSQL")

    rows = {r["scope_id"]: r for r in importer.stocktake(cur)}
    assert rows[scope_id]["state"] == 0
    assert rows[scope_id]["migrated"] is False
    assert rows[scope_id]["pending"] == 1


def test_a_retired_entity_is_not_counted_as_outstanding_work(cur, scope_id):
    """No amount of reviewing clears a backlog made of rejected memories."""
    from mashu.models import VersionStatus

    _author(cur, scope_id, MemoryType.STATE, "where the work stands", "phase 2 is in")
    memory_id, version_id = _author(
        cur, scope_id, MemoryType.HYPOTHESIS, "a guess", "it may be the bridge chip", adopt=False
    )
    rows = {r["scope_id"]: r for r in importer.stocktake(cur)}
    assert rows[scope_id]["pending"] == 1

    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DORMANT,
        actor="user",
        reason="the measurement ruled it out",
    )
    rows = {r["scope_id"]: r for r in importer.stocktake(cur)}
    assert rows[scope_id]["pending"] == 0
    assert store.get_entity(cur, memory_id)["active_version"] is None
