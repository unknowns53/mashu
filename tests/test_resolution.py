"""Entity resolution: one concept, one entity (specification 20).

A duplicate is dangerous in a way a conflict is not. Two entities for the same
concept both answer as current, and nothing in the state marks them as
disagreeing, so the split never surfaces on its own.
"""

from __future__ import annotations

import pytest

from mashu import proposals, resolution, store
from mashu.models import EntityStatus, MemoryType, ProposalOperation, SourceType


@pytest.fixture
def author(cur, scope_id):
    def _author(title, content="the enclosure bridge chip times out"):
        return store.create_entity(
            cur,
            scope_id=scope_id,
            type=MemoryType.OBSERVATION,
            title=title,
            content=content,
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=True,
        )

    return _author


def _create(cur, scope_id, title, **over):
    payload = {
        "scope_id": str(scope_id),
        "type": str(MemoryType.OBSERVATION),
        "title": title,
        "content": "the drive times out over the enclosure",
        "source_type": str(SourceType.AGENT),
    }
    payload.update(over)
    return proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload=payload,
        **{k: v for k, v in over.pop("_kw", {}).items()},
    )


def test_a_confusable_title_offers_the_existing_entities_first(cur, scope_id, author):
    """Which of the confusable pair ranks first is the model's business.

    What this layer owes is that a confusable title comes back at all, in
    similarity order, and only above the threshold. Ranking quality is what
    27.2 measures, against the model rather than against the stand-in used
    here.
    """
    author("SSD failure analysis")
    author("SSD debugging")
    author("cloud point ramp rate")

    found = resolution.find_similar(cur, scope_id=scope_id, title="SSD debug analysis")
    assert found, "a confusable title has to surface something"
    assert "cloud point ramp rate" not in {row["title"] for row in found}
    assert all(row["similarity"] >= resolution.threshold() for row in found)
    assert [row["similarity"] for row in found] == sorted(
        (row["similarity"] for row in found), reverse=True
    )


def test_an_unrelated_title_offers_nothing(cur, scope_id, author):
    author("SSD failure analysis")
    assert resolution.find_similar(cur, scope_id=scope_id, title="cloud point ramp rate") == []


def test_proposing_a_confusable_title_stops_and_shows_what_exists(cur, scope_id, author):
    author("SSD debugging")
    with pytest.raises(resolution.SimilarEntityError) as caught:
        _create(cur, scope_id, "SSD debugging")
    assert [row["title"] for row in caught.value.candidates] == ["SSD debugging"]


def test_creating_anyway_above_the_threshold_goes_to_review(cur, scope_id, author):
    """Section 20 lets the agent create; 20.1 makes the result provisional."""
    author("SSD debugging")
    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.OBSERVATION),
            "title": "SSD debugging",
            "content": "a different investigation with the same name",
            "source_type": str(SourceType.AGENT),
        },
        allow_similar=True,
        allow_duplicate=True,
    )

    entity = store.get_entity(cur, result["proposal"]["target_memory"])
    assert entity["status"] == EntityStatus.PROVISIONAL
    assert entity["active_version"] is None

    queue = proposals.session_queue(cur)
    assert [p["title"] for b in queue for p in b["proposals"]] == ["SSD debugging"]


def test_a_provisional_entity_stays_out_of_layer_one(cur, scope_id, author):
    from mashu import retrieval

    author("SSD debugging", "the enclosure bridge chip times out")
    proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.OBSERVATION),
            "title": "SSD debugging",
            "content": "a different investigation with the same name",
            "source_type": str(SourceType.AGENT),
        },
        allow_similar=True,
        allow_duplicate=True,
    )

    got = retrieval.retrieve(cur, "SSD debugging", actor="claude", scope_id=scope_id)
    assert [row["title"] for row in got.active] == ["SSD debugging"]
    assert "different investigation" not in got.active[0]["content"]
    assert any("different investigation" in row["content"] for row in got.unreviewed)


def test_an_unrelated_title_is_created_outright(cur, scope_id, author):
    author("SSD failure analysis")
    result = _create(cur, scope_id, "cloud point ramp rate")
    entity = store.get_entity(cur, result["proposal"]["target_memory"])
    assert entity["status"] == EntityStatus.ACTIVE


def test_resolution_does_not_look_across_scopes(cur, scope_id, author):
    """Scopes exist so that the same words in two projects stay apart."""
    author("SSD failure analysis")
    other = store.create_scope(cur, name="another project", actor="user")
    assert resolution.find_similar(cur, scope_id=other, title="SSD failure analysis") == []


def test_an_archived_entity_is_not_offered(cur, scope_id, author):
    """Reviving what the user archived should not happen by accident."""
    memory_id, _ = author("SSD failure analysis")
    store.set_entity_status(
        cur,
        memory_id=memory_id,
        target=EntityStatus.ARCHIVED,
        actor="user",
        reason="the investigation closed",
    )
    assert resolution.find_similar(cur, scope_id=scope_id, title="SSD failure analysis") == []


def test_the_threshold_can_be_set_from_the_environment(cur, monkeypatch):
    """27.2 measures it per model; until then it has to be movable."""
    monkeypatch.setenv(resolution.THRESHOLD_ENV_VAR, "0.99")
    assert resolution.threshold() == 0.99
