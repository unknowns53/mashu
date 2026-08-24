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


def test_a_look_alike_is_found_across_the_scope(cur, scope_id, author):
    """20 keeps one concept from answering twice; review is where a person can see it."""
    author("送風の設定は測定から決める", "推測で置いた値は下流へ渡さない")
    twin, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.DECISION,
        title="送風の設定は測定から決める",
        content="別の言い方で同じことを言っている",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=False,
    )

    found = resolution.look_alikes(cur, scope_id=scope_id, memory_ids=[twin])
    assert twin in found, "an identical title in the same scope has to come back"
    assert found[twin][0]["title"] == "送風の設定は測定から決める"
    assert found[twin][0]["similarity"] > resolution.LOOK_ALIKE_FLOOR


def test_nothing_comes_back_for_a_memory_standing_on_its_own(cur, scope_id, author):
    """A note in the margin that appears on every item is not a note, it is noise."""
    author("SSD failure analysis")
    author("cloud point ramp rate")
    alone, _ = author("まったく関係のない話題、たとえば昼食の献立", "今日は蕎麦だった")
    assert resolution.look_alikes(cur, scope_id=scope_id, memory_ids=[alone]) == {}


def test_a_memory_is_never_its_own_look_alike(cur, scope_id, author):
    memory_id, _ = author("ひとつしかない題名")
    found = resolution.look_alikes(cur, scope_id=scope_id, memory_ids=[memory_id], floor=-1.0)
    assert memory_id not in [row["memory_id"] for row in found.get(memory_id, [])]


def test_a_look_alike_among_what_is_adopted_is_found_after_review(cur, scope_id, author):
    """The check in 20 is spent by review time, and what got through is never measured again.

    Ten of seventy-seven memories in one scope of the real store had a
    look-alike beside them, and nothing was in a position to say so.
    """
    author("送風の設定は測定から決める", "推測で置いた値は下流へ渡さない")
    author("送風の設定は測定から決める", "別の言い方で同じことを言っている")

    pairs = resolution.adopted_pairs(cur)
    assert len(pairs) == 1
    assert pairs[0]["same_title"] is True
    assert {pairs[0]["left"]["title"], pairs[0]["right"]["title"]} == {"送風の設定は測定から決める"}


def test_a_pair_comes_back_once_and_not_from_both_sides(cur, scope_id, author):
    """The decision is about the two together, so the order shown is not part of it."""
    author("測定の順序を決める", "先に空試験をする")
    author("測定の順序を決める", "空試験を先に回す")
    assert len(resolution.adopted_pairs(cur)) == 1


def test_telling_a_pair_apart_stops_it_being_offered(cur, scope_id, author):
    """Without this the screen can never be finished: alike things stay alike."""
    first, _ = author("温度の読み方", "外側の値を採る")
    second, _ = author("温度の読み方", "内側の値を採る")
    assert resolution.adopted_pairs(cur)

    store.tell_apart(
        cur, memory_id=first, other=second, actor="user", reason="測る場所が違う二つの話"
    )
    assert resolution.adopted_pairs(cur) == []


def test_a_pair_whose_other_half_is_unreviewed_is_review_business(cur, scope_id, author):
    """The sweep is over what the store holds as true; a candidate is not that yet."""
    author("既に採用された題", "採用された本文")
    store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.OBSERVATION,
        title="既に採用された題",
        content="まだ採用されていない本文",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=False,
    )
    assert resolution.adopted_pairs(cur) == []


def test_nothing_comes_back_when_the_store_holds_one_of_everything(cur, scope_id, author):
    author("SSD failure analysis")
    author("まったく関係のない話題、たとえば昼食の献立", "今日は蕎麦だった")
    assert resolution.adopted_pairs(cur) == []
