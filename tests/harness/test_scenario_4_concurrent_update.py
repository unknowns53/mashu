"""Scenario 4: two changes written against the same state, one of them refused.

    Two actors read the same entity and each proposes a change. The first lands.
    The second was written against a state that has since moved and must be
    refused rather than merged, so the second actor re-reads and proposes again.

Specification 24. Only one agent is connected in the MVP, so this cannot happen
in practice yet; the structure is in place from the start because retrofitting a
lock after the fact means auditing every write path.
"""

import pytest

from mashu import store
from mashu.errors import ConcurrentUpdateError
from mashu.models import SourceType


def test_the_second_writer_is_refused(cur, author):
    memory_id, first = author("shared entity", "the original reading")
    both_read = first  # what each actor saw before writing

    store.add_version(
        cur,
        memory_id=memory_id,
        content="the first actor's correction",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=both_read,
        adopt=True,
    )

    with pytest.raises(ConcurrentUpdateError):
        store.add_version(
            cur,
            memory_id=memory_id,
            content="the second actor's correction",
            source_type=SourceType.AGENT,
            created_by="codex",
            actor="codex",
            based_on_version=both_read,
            adopt=True,
        )


def test_the_refusal_leaves_the_first_change_intact(cur, author):
    """A refused write must not have partially landed."""
    memory_id, first = author("shared entity", "the original reading")
    winner = store.add_version(
        cur,
        memory_id=memory_id,
        content="the first actor's correction",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=True,
    )
    with pytest.raises(ConcurrentUpdateError):
        store.add_version(
            cur,
            memory_id=memory_id,
            content="the second actor's correction",
            source_type=SourceType.AGENT,
            created_by="codex",
            actor="codex",
            based_on_version=first,
            adopt=True,
        )

    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] == winner
    assert entity["latest_version"] == winner
    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    assert cur.fetchone()["n"] == 2


def test_re_reading_lets_the_second_writer_through(cur, author):
    """The refusal is a retry instruction, not a dead end."""
    memory_id, first = author("shared entity", "the original reading")
    store.add_version(
        cur,
        memory_id=memory_id,
        content="the first actor's correction",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=True,
    )

    current = store.get_entity(cur, memory_id)["latest_version"]
    second = store.add_version(
        cur,
        memory_id=memory_id,
        content="the second actor's correction, rewritten",
        source_type=SourceType.AGENT,
        created_by="codex",
        actor="codex",
        based_on_version=current,
        adopt=True,
    )
    assert store.get_entity(cur, memory_id)["active_version"] == second
