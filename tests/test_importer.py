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
    """Nothing arrives already trusted, whichever rule does the holding.

    An imported preference is now held twice over: by 27.1, which holds the
    whole migration, and by the gate, which stopped treating the type alone as
    a licence. The assertion is on the outcome rather than on which rule got
    there first, because either one failing should still leave it pending.
    """
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


def test_imported_content_is_readable_before_anything_is_reviewed(cur, scope_id):
    """Semi-approval reaches a scope whose every memory is imported.

    This test held the opposite for two versions, and the collision was left
    pinned on purpose: v0.7 made layer 2 hand over content so that waiting for
    review was not an outage, v0.8 then capped layer 2 at the size of layer 1,
    and a freshly imported scope has no layer 1 at all. Which rule yielded was
    a decision about 21.1 rather than something to settle by editing an
    assertion here.

    v0.11 settled it. The cap was withdrawn and v0.7 stands: an import is
    readable, tagged, from the moment it lands.
    """
    importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("SSD failure analysis", "the enclosure bridge chip times out")],
        actor="import",
    )
    got = retrieval.retrieve(cur, "SSD failure analysis", actor="claude", scope_id=scope_id)
    assert got.active == []
    assert [row["title"] for row in got.unreviewed] == ["SSD failure analysis"]
    assert "bridge chip" in got.unreviewed[0]["content"]
    assert got.unreviewed[0]["tag"] == retrieval.UNREVIEWED_TAG


def test_the_short_standing_form_comes_across_when_the_file_marked_one(cur, scope_id):
    """21.2 pushes the directive; the migration is where the store gets one.

    Taken as written from the source rather than summarised here. A summary
    made during the import is an interpretation entering the store by the one
    route that does not pass a review.
    """
    item = _item("measurement discipline", "the whole rule with its reasons attached")
    item["directive"] = "say what each branch will lead to before starting"
    summary = importer.import_items(cur, scope_id=scope_id, items=[item], actor="import")

    proposal = proposals.get(cur, summary["created"][0]["proposal_id"])
    version = store.get_version(cur, proposal["applied_version"])
    assert version["directive"] == "say what each branch will lead to before starting"
    assert version["content"] == "the whole rule with its reasons attached"


def test_an_item_without_one_keeps_a_null_directive(cur, scope_id):
    summary = importer.import_items(
        cur,
        scope_id=scope_id,
        items=[_item("measurement discipline", "the whole rule")],
        actor="import",
    )
    proposal = proposals.get(cur, summary["created"][0]["proposal_id"])
    assert store.get_version(cur, proposal["applied_version"])["directive"] is None
