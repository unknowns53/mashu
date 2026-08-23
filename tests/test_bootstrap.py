"""Session Bootstrap (specification 21.2)."""

from __future__ import annotations

import pytest

from mashu import bootstrap, store
from mashu.models import EventType, MemoryType, SourceType


@pytest.fixture
def write(cur, scope_id):
    def _write(type, title, content, scope=None):
        return store.create_entity(
            cur,
            scope_id=scope or scope_id,
            type=type,
            title=title,
            content=content,
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            adopt=True,
        )

    return _write


# --------------------------------------------------------------------------
# the three parts
# --------------------------------------------------------------------------
def test_the_scope_index_lists_every_active_scope_with_its_summary(cur, scope_id):
    store.create_scope(
        cur,
        name="DES thermal response",
        description="cloud point behaviour of the mixtures",
        actor="user",
    )
    got = bootstrap.session_bootstrap(cur, actor="claude")

    index = {row["name"]: row["summary"] for row in got.scope_index}
    assert "DES thermal response" in index
    assert index["DES thermal response"] == "cloud point behaviour of the mixtures"


def test_an_archived_scope_is_not_on_the_map(cur, scope_id):
    """The index says what is worth asking about, and archived scopes are not."""
    other = store.create_scope(cur, name="the old rig", actor="user")
    cur.execute("UPDATE scope SET status = 'archived' WHERE scope_id = %s", (other,))

    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert other not in [row["scope_id"] for row in got.scope_index]


def test_preferences_and_current_state_arrive_whole(cur, write):
    write(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    write(MemoryType.STATE, "where the analysis stands", "the third run is still going")

    got = bootstrap.session_bootstrap(cur, actor="claude")

    assert [row["content"] for row in got.preferences] == ["answer in Japanese"]
    assert [row["content"] for row in got.current_state] == ["the third run is still going"]
    assert got.trimmed == []


def test_nothing_but_preferences_and_state_is_pushed(cur, write):
    """Everything else is pull. Bootstrap is a map, not a dump of the store."""
    write(MemoryType.FACT, "a fact", "the mixture is hygroscopic")
    write(MemoryType.HYPOTHESIS, "a hypothesis", "the bridge chip is at fault")

    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.preferences == []
    assert got.current_state == []
    assert got.scope_index != []


def test_a_memory_with_no_current_truth_is_not_pushed(cur, scope_id, write):
    """Bootstrap reads the pointer, so a candidate awaiting review stays out."""
    store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.PREFERENCE,
        title="reply language",
        content="answer in Japanese",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=False,
    )
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.preferences == []


def test_the_state_narrows_to_the_scopes_a_session_names(cur, scope_id, write):
    elsewhere = store.create_scope(cur, name="another scope", actor="user")
    write(MemoryType.STATE, "here", "the third run is still going")
    write(MemoryType.STATE, "there", "nothing has started", scope=elsewhere)

    got = bootstrap.session_bootstrap(cur, actor="claude", scopes=[scope_id])
    assert [row["title"] for row in got.current_state] == ["here"]
    # The map still shows the whole ledger; narrowing the state is not hiding
    # that the other scope exists.
    assert elsewhere in [row["scope_id"] for row in got.scope_index]


# --------------------------------------------------------------------------
# the token ceiling
# --------------------------------------------------------------------------
def test_the_map_survives_the_ceiling_and_the_bodies_do_not(cur, write):
    """21.2: cut content, keep the index, because the index is what reaches it."""
    write(MemoryType.PREFERENCE, "reply language", "answer in Japanese " * 200)
    write(MemoryType.STATE, "where the analysis stands", "the third run " * 200)

    got = bootstrap.session_bootstrap(cur, actor="claude", budget=100)

    assert got.scope_index != []
    assert [row["summary"] is not None for row in got.scope_index] == [True]
    assert got.trimmed != []
    for row in (*got.preferences, *got.current_state):
        if row["memory_id"] in got.trimmed:
            assert row["content"] is None
            assert row["title"]


def test_the_current_state_gives_up_its_body_before_a_preference_does(cur, write):
    """A preference nobody can read is misbehaviour that nobody notices."""
    pref, _ = write(MemoryType.PREFERENCE, "reply language", "answer in Japanese " * 40)
    state, _ = write(MemoryType.STATE, "where the analysis stands", "the third run " * 40)

    got = bootstrap.session_bootstrap(cur, actor="claude", budget=250)

    assert state in got.trimmed
    assert pref not in got.trimmed


def test_an_index_that_alone_exceeds_the_ceiling_says_so(cur, write):
    """21.2 never trims the index, so past a certain ledger there is no way down.

    Silently returning an over-budget payload would make the one number the
    switchover trial is meant to re-measure a number that is quietly wrong.
    """
    for i in range(12):
        store.create_scope(
            cur,
            name=f"a scope with a long summary {i}",
            description="something wordy enough to matter " * 12,
            actor="user",
        )

    got = bootstrap.session_bootstrap(cur, actor="claude", budget=200)
    assert got.tokens > 200
    assert got.over_budget is True
    assert got.trimmed == [], "there was no content to trim; the index is not trimmable"


def test_a_payload_inside_the_budget_is_left_alone(cur, write):
    write(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.trimmed == []
    assert got.tokens <= bootstrap.BOOTSTRAP_TOKEN_BUDGET
    assert got.over_budget is False


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------
def test_what_was_handed_over_is_logged(cur, write):
    """22: the same obligation as context assembly, so the fixed cost is visible."""
    pref, _ = write(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = %s",
        (str(EventType.SESSION_BOOTSTRAPPED),),
    )
    detail = cur.fetchone()["detail"]
    assert detail["memories"] == [str(pref)]
    assert detail["tokens"] == got.tokens
    assert detail["trimmed"] == []


def test_bootstrap_never_reaches_for_the_model(cur, write, monkeypatch):
    """There is no query to encode, and session start is where a cold load hurts."""
    import mashu.embed

    def refuse():
        raise AssertionError("bootstrap must not need an embedder")

    monkeypatch.setattr(mashu.embed, "get_embedder", refuse)
    write(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    assert bootstrap.session_bootstrap(cur, actor="claude").preferences
