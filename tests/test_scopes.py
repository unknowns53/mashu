"""Scope readiness and lifecycle (specification 7.1)."""

from __future__ import annotations

import pytest

from mashu import scopes, store
from mashu.errors import MashuError
from mashu.models import EventType, Lifecycle, MemoryType, SourceType


@pytest.fixture
def write(cur, scope_id):
    def _write(type, title, content, adopt=True):
        return store.create_entity(
            cur,
            scope_id=scope_id,
            type=type,
            title=title,
            content=content,
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            adopt=adopt,
        )

    return _write


def _of(cur, scope_id):
    return next(row for row in scopes.readiness(cur, scope_id))


def test_a_new_scope_starts_seeding(cur, scope_id):
    """Not knowing anything and not being open yet are different states."""
    row = _of(cur, scope_id)
    assert row["lifecycle"] is Lifecycle.SEEDING
    assert row["missing"] == ["state", "preference", "decision"]


def test_a_scope_will_not_open_while_what_it_declared_is_absent(cur, scope_id):
    with pytest.raises(scopes.NotReadyError) as refused:
        scopes.promote(cur, scope_id=scope_id, actor="user")
    assert set(refused.value.missing) == {"state", "preference", "decision"}
    assert _of(cur, scope_id)["lifecycle"] is Lifecycle.SEEDING


def test_a_type_can_be_declared_unnecessary_instead_of_invented(cur, scope_id, write):
    """A fixed count of three would make a scope with no decisions invent one."""
    write(MemoryType.STATE, "where the work stands", "the third run is going")
    for absent in (MemoryType.PREFERENCE, MemoryType.DECISION):
        scopes.set_requirement(
            cur, scope_id=scope_id, type=absent, requirement=scopes.NOT_NEEDED, actor="user"
        )

    opened = scopes.promote(cur, scope_id=scope_id, actor="user")
    assert opened["lifecycle"] is Lifecycle.OPERATIONAL


def test_an_unreviewed_memory_does_not_count_towards_readiness(cur, scope_id, write):
    """Readiness is evidence that a person adopted something, not that one exists."""
    write(MemoryType.STATE, "where the work stands", "the third run is going", adopt=False)
    assert "state" in _of(cur, scope_id)["missing"]


def test_only_the_three_of_section_27_1_gate_promotion(cur, scope_id):
    with pytest.raises(MashuError, match="not part of readiness"):
        scopes.set_requirement(
            cur,
            scope_id=scope_id,
            type=MemoryType.HYPOTHESIS,
            requirement=scopes.NOT_NEEDED,
            actor="user",
        )


def test_a_requirement_is_one_of_two_words(cur, scope_id):
    with pytest.raises(MashuError, match="required or not_needed"):
        scopes.set_requirement(
            cur, scope_id=scope_id, type=MemoryType.STATE, requirement="maybe", actor="user"
        )


def test_promotion_is_recorded(cur, scope_id, write):
    write(MemoryType.STATE, "where the work stands", "the third run is going")
    for absent in (MemoryType.PREFERENCE, MemoryType.DECISION):
        scopes.set_requirement(
            cur, scope_id=scope_id, type=absent, requirement=scopes.NOT_NEEDED, actor="user"
        )
    scopes.promote(cur, scope_id=scope_id, actor="user")

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = %s",
        (str(EventType.SCOPE_PROMOTED),),
    )
    assert cur.fetchone()["detail"]["scope_id"] == str(scope_id)


def test_promoting_an_open_scope_changes_nothing(cur, scope_id, write):
    write(MemoryType.STATE, "where the work stands", "the third run is going")
    for absent in (MemoryType.PREFERENCE, MemoryType.DECISION):
        scopes.set_requirement(
            cur, scope_id=scope_id, type=absent, requirement=scopes.NOT_NEEDED, actor="user"
        )
    scopes.promote(cur, scope_id=scope_id, actor="user")
    again = scopes.promote(cur, scope_id=scope_id, actor="user")
    assert again["lifecycle"] is Lifecycle.OPERATIONAL

    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = %s",
        (str(EventType.SCOPE_PROMOTED),),
    )
    assert cur.fetchone()["n"] == 1
