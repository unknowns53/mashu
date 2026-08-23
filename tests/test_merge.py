"""Merging two entities into one (specification 20.2)."""

import pytest

from mashu import store
from mashu.errors import MergeError
from mashu.models import EntityStatus, EventType, MemoryType, SourceType, VersionStatus


def entity_with_version(cur, scope_id, title, content, *, adopt=True, entity_status=None):
    kwargs = {}
    if entity_status is not None:
        kwargs["entity_status"] = entity_status
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.OBSERVATION,
        title=title,
        content=content,
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=adopt,
        **kwargs,
    )


def versions_of(cur, memory_id):
    cur.execute("SELECT version_id FROM memory_version WHERE memory_id = %s", (memory_id,))
    return {row["version_id"] for row in cur.fetchall()}


# --------------------------------------------------------------------------
# moving the content
# --------------------------------------------------------------------------
def test_the_versions_move_to_the_target(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, absorb_v = entity_with_version(cur, scope_id, "SSD debugging", "more notes")

    store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="the same investigation under two names",
        keep_active=keep_v,
    )

    assert versions_of(cur, keep) == {keep_v, absorb_v}
    assert versions_of(cur, absorb) == set()


def test_the_source_is_kept_and_points_at_the_target(cur, scope_id):
    """Past context recorded the source id, so the row has to stay resolvable."""
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, _ = entity_with_version(cur, scope_id, "SSD debugging", "more notes")

    store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="duplicate",
        keep_active=keep_v,
    )

    row = store.get_entity(cur, absorb)
    assert row["status"] == EntityStatus.MERGED
    assert row["merged_into"] == keep
    assert row["active_version"] is None
    assert row["latest_version"] is None


# --------------------------------------------------------------------------
# choosing which reading survives
# --------------------------------------------------------------------------
def test_two_active_versions_force_a_choice(cur, scope_id):
    keep, _ = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, _ = entity_with_version(cur, scope_id, "SSD debugging", "more notes")

    with pytest.raises(MergeError, match="name which one survives"):
        store.merge_entities(cur, source=absorb, target=keep, actor="user", reason="duplicate")


def test_the_reading_not_chosen_is_superseded_with_its_reason(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, absorb_v = entity_with_version(cur, scope_id, "SSD debugging", "more notes")

    store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="the enclosure notes were the fuller ones",
        keep_active=absorb_v,
    )

    assert store.get_entity(cur, keep)["active_version"] == absorb_v
    retired = store.get_version(cur, keep_v)
    assert retired["status"] == VersionStatus.SUPERSEDED
    assert "the enclosure notes were the fuller ones" in retired["reason"]


def test_a_source_without_an_active_version_needs_no_choice(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, _ = entity_with_version(cur, scope_id, "SSD debugging", "unreviewed", adopt=False)

    store.merge_entities(cur, source=absorb, target=keep, actor="user", reason="duplicate")
    assert store.get_entity(cur, keep)["active_version"] == keep_v


def test_the_latest_pointer_covers_the_merged_history(cur, scope_id):
    keep, _ = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, absorb_v = entity_with_version(cur, scope_id, "SSD debugging", "newer notes")

    store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="duplicate",
        keep_active=absorb_v,
    )
    assert store.get_entity(cur, keep)["latest_version"] in versions_of(cur, keep)


# --------------------------------------------------------------------------
# evidence edges
# --------------------------------------------------------------------------
def test_evidence_pointing_at_the_source_is_repointed(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, _ = entity_with_version(cur, scope_id, "SSD debugging", "more notes")
    citing, citing_v = entity_with_version(cur, scope_id, "report", "a conclusion")
    cur.execute(
        "INSERT INTO memory_evidence (from_version, to_memory) VALUES (%s, %s)",
        (citing_v, absorb),
    )

    summary = store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="duplicate",
        keep_active=keep_v,
    )

    cur.execute("SELECT to_memory FROM memory_evidence WHERE from_version = %s", (citing_v,))
    assert [row["to_memory"] for row in cur.fetchall()] == [keep]
    assert summary["evidence_moved"] == 1
    assert citing  # the citing entity is untouched


def test_evidence_citing_both_entities_collapses_to_one_edge(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, _ = entity_with_version(cur, scope_id, "SSD debugging", "more notes")
    _, citing_v = entity_with_version(cur, scope_id, "report", "a conclusion")
    cur.executemany(
        "INSERT INTO memory_evidence (from_version, to_memory) VALUES (%s, %s)",
        [(citing_v, keep), (citing_v, absorb)],
    )

    summary = store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="duplicate",
        keep_active=keep_v,
    )

    cur.execute("SELECT count(*) AS n FROM memory_evidence WHERE from_version = %s", (citing_v,))
    assert cur.fetchone()["n"] == 1
    assert summary["evidence_deduplicated"] == 1


# --------------------------------------------------------------------------
# refusals and the log
# --------------------------------------------------------------------------
def test_an_entity_cannot_be_merged_into_itself(cur, scope_id):
    only, _ = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    with pytest.raises(MergeError, match="into itself"):
        store.merge_entities(cur, source=only, target=only, actor="user", reason="duplicate")


def test_a_merged_entity_cannot_be_merged_again(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, _ = entity_with_version(cur, scope_id, "SSD debugging", "more notes")
    third, _ = entity_with_version(cur, scope_id, "external SSD problem", "yet more")

    store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="duplicate",
        keep_active=keep_v,
    )
    with pytest.raises(MergeError, match="already merged"):
        store.merge_entities(cur, source=absorb, target=third, actor="user", reason="duplicate")


def test_the_merge_is_logged_with_what_it_moved(cur, scope_id):
    keep, keep_v = entity_with_version(cur, scope_id, "SSD failure analysis", "notes")
    absorb, absorb_v = entity_with_version(cur, scope_id, "SSD debugging", "more notes")

    store.merge_entities(
        cur,
        source=absorb,
        target=keep,
        actor="user",
        reason="the same investigation under two names",
        keep_active=keep_v,
    )

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = %s",
        (str(EventType.ENTITY_MERGED),),
    )
    detail = cur.fetchone()["detail"]
    assert detail["source"] == str(absorb)
    assert detail["versions_moved"] == 1
    assert detail["not_chosen"] == [str(absorb_v)]


# --------------------------------------------------------------------------
# entity status on its own
# --------------------------------------------------------------------------
def test_a_provisional_entity_is_created_and_usable(cur, scope_id):
    """Specification 20.1: review pending must not block the agent."""
    memory_id, version_id = entity_with_version(
        cur,
        scope_id,
        "SSD debugging",
        "notes",
        entity_status=EntityStatus.PROVISIONAL,
    )
    assert store.get_entity(cur, memory_id)["status"] == EntityStatus.PROVISIONAL
    assert store.get_entity(cur, memory_id)["active_version"] == version_id


def test_review_can_settle_a_provisional_entity(cur, scope_id):
    memory_id, _ = entity_with_version(
        cur, scope_id, "SSD debugging", "notes", entity_status=EntityStatus.PROVISIONAL
    )
    store.set_entity_status(
        cur,
        memory_id=memory_id,
        target=EntityStatus.ACTIVE,
        actor="user",
        reason="a genuinely separate investigation",
    )
    assert store.get_entity(cur, memory_id)["status"] == EntityStatus.ACTIVE


def test_merging_is_not_reachable_through_a_status_change(cur, scope_id):
    memory_id, _ = entity_with_version(cur, scope_id, "SSD debugging", "notes")
    with pytest.raises(MergeError, match="merge_entities"):
        store.set_entity_status(
            cur,
            memory_id=memory_id,
            target=EntityStatus.MERGED,
            actor="user",
            reason="duplicate",
        )
