"""The store against a real database: pointers, statuses and the event log."""

import psycopg
import pytest

from mashu import store
from mashu.errors import ActivePointerError, ConcurrentUpdateError, NotFoundError
from mashu.models import EventType, MemoryType, SourceType, VersionStatus


def make_entity(cur, scope_id, *, adopt, content="first"):
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.FACT,
        title="a memory",
        content=content,
        source_type=SourceType.USER,
        created_by="tester",
        actor="tester",
        adopt=adopt,
    )


def events_of(cur, event_type):
    cur.execute(
        "SELECT * FROM event_log WHERE event_type = %s ORDER BY event_id",
        (str(event_type),),
    )
    return cur.fetchall()


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------
def test_a_candidate_that_is_not_adopted_leaves_the_entity_without_a_truth(cur, scope_id):
    """Specification 17: a Candidate Commit waits for review, it does not land."""
    memory_id, version_id = make_entity(cur, scope_id, adopt=False)
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] is None
    assert entity["latest_version"] == version_id
    assert store.get_version(cur, version_id)["status"] == VersionStatus.CANDIDATE


def test_an_adopted_first_version_becomes_the_active_one(cur, scope_id):
    memory_id, version_id = make_entity(cur, scope_id, adopt=True)
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] == version_id
    assert entity["latest_version"] == version_id


def test_creation_is_written_to_the_event_log(cur, scope_id):
    memory_id, version_id = make_entity(cur, scope_id, adopt=True)
    assert [e["memory_id"] for e in events_of(cur, EventType.ENTITY_CREATED)] == [memory_id]
    assert [e["version_id"] for e in events_of(cur, EventType.VERSION_CREATED)] == [version_id]
    switched = events_of(cur, EventType.ACTIVE_SWITCHED)
    assert len(switched) == 1
    assert switched[0]["detail"]["from"] is None


# --------------------------------------------------------------------------
# adopting a newer version
# --------------------------------------------------------------------------
def test_adopting_a_new_version_retires_the_previous_one(cur, scope_id):
    memory_id, first = make_entity(cur, scope_id, adopt=True)
    second = store.add_version(
        cur,
        memory_id=memory_id,
        content="corrected",
        source_type=SourceType.USER,
        created_by="tester",
        actor="tester",
        based_on_version=first,
        adopt=True,
        reason="the first reading was wrong",
    )
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] == second
    assert store.get_version(cur, first)["status"] == VersionStatus.SUPERSEDED
    assert store.get_version(cur, second)["supersedes"] == first


def test_a_stale_base_version_is_refused(cur, scope_id):
    """Specification 24: the optimistic lock, checked at commit time."""
    memory_id, first = make_entity(cur, scope_id, adopt=True)
    store.add_version(
        cur,
        memory_id=memory_id,
        content="second",
        source_type=SourceType.AGENT,
        created_by="agent",
        actor="agent",
        based_on_version=first,
        adopt=True,
    )
    with pytest.raises(ConcurrentUpdateError, match="re-read and propose again"):
        store.add_version(
            cur,
            memory_id=memory_id,
            content="written against a state that has moved",
            source_type=SourceType.AGENT,
            created_by="agent",
            actor="agent",
            based_on_version=first,
            adopt=True,
        )


def test_a_candidate_added_without_adopting_leaves_the_pointer_alone(cur, scope_id):
    memory_id, first = make_entity(cur, scope_id, adopt=True)
    second = store.add_version(
        cur,
        memory_id=memory_id,
        content="proposed",
        source_type=SourceType.AGENT,
        created_by="agent",
        actor="agent",
        based_on_version=first,
        adopt=False,
    )
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] == first
    assert entity["latest_version"] == second
    assert store.get_version(cur, first)["status"] == VersionStatus.CANDIDATE


# --------------------------------------------------------------------------
# status changes
# --------------------------------------------------------------------------
def test_disproving_the_active_version_leaves_no_current_truth(cur, scope_id):
    memory_id, version_id = make_entity(cur, scope_id, adopt=True)
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="measurement contradicted it",
    )
    assert store.get_entity(cur, memory_id)["active_version"] is None
    assert store.get_version(cur, version_id)["status"] == VersionStatus.DISPROVEN


def test_completing_a_task_keeps_it_active(cur, scope_id):
    """Specification 1: a finished task must not read as unfinished."""
    memory_id, version_id = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.TASK,
        title="run the sweep",
        content="sweep the temperature range",
        source_type=SourceType.USER,
        created_by="tester",
        actor="tester",
        adopt=True,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.COMPLETED,
        actor="user",
        reason="the sweep finished",
    )
    assert store.get_entity(cur, memory_id)["active_version"] == version_id


def test_the_reason_for_a_status_change_is_logged(cur, scope_id):
    _, version_id = make_entity(cur, scope_id, adopt=True)
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DORMANT,
        actor="user",
        reason="parked until the next run",
    )
    changed = events_of(cur, EventType.STATUS_CHANGED)
    assert changed[-1]["detail"]["reason"] == "parked until the next run"
    assert changed[-1]["detail"]["to"] == "dormant"


# --------------------------------------------------------------------------
# moving the pointer by hand
# --------------------------------------------------------------------------
def test_a_superseded_version_cannot_be_made_active_again(cur, scope_id):
    memory_id, first = make_entity(cur, scope_id, adopt=True)
    store.add_version(
        cur,
        memory_id=memory_id,
        content="second",
        source_type=SourceType.USER,
        created_by="tester",
        actor="tester",
        based_on_version=first,
        adopt=True,
    )
    with pytest.raises(ActivePointerError, match="cannot be active"):
        store.set_active(cur, memory_id=memory_id, version_id=first, actor="user")


def test_a_version_of_another_entity_cannot_be_made_active(cur, scope_id):
    memory_a, _ = make_entity(cur, scope_id, adopt=True)
    _, version_b = make_entity(cur, scope_id, adopt=False, content="other")
    with pytest.raises(NotFoundError, match="belongs to"):
        store.set_active(cur, memory_id=memory_a, version_id=version_b, actor="user")


def test_adopting_a_pending_candidate_supersedes_the_one_in_place(cur, scope_id):
    memory_id, first = make_entity(cur, scope_id, adopt=True)
    second = store.add_version(
        cur,
        memory_id=memory_id,
        content="reviewed and approved",
        source_type=SourceType.AGENT,
        created_by="agent",
        actor="agent",
        based_on_version=first,
        adopt=False,
    )
    store.set_active(
        cur,
        memory_id=memory_id,
        version_id=second,
        actor="user",
        reason="approved at review",
    )
    assert store.get_entity(cur, memory_id)["active_version"] == second
    assert store.get_version(cur, first)["status"] == VersionStatus.SUPERSEDED


# --------------------------------------------------------------------------
# the log itself
# --------------------------------------------------------------------------
def test_the_event_log_refuses_to_be_rewritten(cur, scope_id):
    make_entity(cur, scope_id, adopt=True)
    with pytest.raises(psycopg.errors.RaiseException, match="append only"):
        cur.execute("UPDATE event_log SET actor = 'someone else'")


def test_the_event_log_refuses_deletion(cur, scope_id):
    make_entity(cur, scope_id, adopt=True)
    with pytest.raises(psycopg.errors.RaiseException, match="append only"):
        cur.execute("DELETE FROM event_log")
