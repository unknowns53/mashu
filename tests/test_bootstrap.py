"""Session Bootstrap (specification 21.2)."""

from __future__ import annotations

import pytest

from mashu import bootstrap, store
from mashu.errors import DeliveryError
from mashu.models import Delivery, EventType, Lifecycle, MemoryType, SourceType


@pytest.fixture
def write(cur, scope_id):
    def _write(type, title, content, scope=None, adopt=True, directive=None):
        return store.create_entity(
            cur,
            scope_id=scope or scope_id,
            type=type,
            title=title,
            content=content,
            directive=directive,
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            adopt=adopt,
        )

    return _write


@pytest.fixture
def push(cur, write):
    """Write a memory and put it in the session-start pack."""

    def _push(type, title, content, directive=None, delivery=Delivery.STARTUP_REQUIRED):
        memory_id, _ = write(type, title, content, directive=directive)
        store.set_delivery(cur, memory_id=memory_id, delivery=delivery, actor="user")
        return memory_id

    return _push


# --------------------------------------------------------------------------
# the map
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
    assert index["DES thermal response"] == "cloud point behaviour of the mixtures"


def test_the_map_says_which_scopes_are_not_open_yet(cur, scope_id):
    """An empty answer from a seeding scope must not read as "nothing is known"."""
    got = bootstrap.session_bootstrap(cur, actor="claude")
    entry = next(row for row in got.scope_index if row["scope_id"] == scope_id)
    assert entry["lifecycle"] == str(Lifecycle.SEEDING)


def test_an_archived_scope_is_not_on_the_map(cur, scope_id):
    other = store.create_scope(cur, name="the old rig", actor="user")
    cur.execute("UPDATE scope SET status = 'archived' WHERE scope_id = %s", (other,))
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert other not in [row["scope_id"] for row in got.scope_index]


# --------------------------------------------------------------------------
# what is pushed, and what is not
# --------------------------------------------------------------------------
def test_being_a_preference_is_not_a_reason_to_push_it(cur, write):
    """The first version pushed every preference, so a fixed cost tracked the store.

    What a memory is and whether every session needs it in front of it are
    different questions, and only the second one spends the opening budget.
    """
    write(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.startup == []
    assert got.scoped == []


def test_what_was_marked_for_the_session_start_is_pushed(cur, push):
    push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert [row["content"] for row in got.startup] == ["answer in Japanese"]


def test_a_current_state_waits_until_the_session_names_its_scope(cur, scope_id, write):
    """State is scope-bound by 14, so it is delivered when the scope is known."""
    write(MemoryType.STATE, "where the analysis stands", "the third run is going")

    blind = bootstrap.session_bootstrap(cur, actor="claude")
    assert blind.scoped == []

    told = bootstrap.session_bootstrap(cur, actor="claude", scopes=[scope_id])
    assert [row["content"] for row in told.scoped] == ["the third run is going"]


def test_the_startup_pack_ignores_the_scope_filter(cur, scope_id, push):
    """It is what a session needs before it knows which scope it is in."""
    push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    elsewhere = store.create_scope(cur, name="another scope", actor="user")
    got = bootstrap.session_bootstrap(cur, actor="claude", scopes=[elsewhere])
    assert [row["title"] for row in got.startup] == ["reply language"]


def test_a_memory_with_no_current_truth_is_not_pushed(cur, scope_id, write):
    """Bootstrap reads the pointer, so a candidate awaiting review stays out."""
    memory_id, _ = write(
        MemoryType.STATE, "where the analysis stands", "the third run is going", adopt=False
    )
    got = bootstrap.session_bootstrap(cur, actor="claude", scopes=[scope_id])
    assert got.scoped == []


# --------------------------------------------------------------------------
# the short form
# --------------------------------------------------------------------------
def test_the_push_takes_the_directive_and_leaves_the_reasons_behind(cur, push):
    """Shortening the opening must not mean losing why the rule exists.

    The directive is the rule as a person reviewed it, not a summary made at
    read time; the content keeps the whole of it for whoever pulls.
    """
    memory_id = push(
        MemoryType.PREFERENCE,
        "measurement discipline",
        "Say what each branch will lead to before starting. Why: a measurement "
        "whose branches lead to the same action was never a question. How to "
        "apply: write X then A, Y then B, and do not start until both differ.",
        directive="Say what each branch will lead to before starting.",
    )
    got = bootstrap.session_bootstrap(cur, actor="claude")
    pushed = got.startup[0]
    assert pushed["content"] == "Say what each branch will lead to before starting."
    assert pushed["shortened"] is True

    whole = store.get_version(cur, store.get_entity(cur, memory_id)["active_version"])
    assert "How to apply" in whole["content"]


def test_without_a_directive_the_whole_content_is_pushed(cur, push):
    push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.startup[0]["shortened"] is False


# --------------------------------------------------------------------------
# the ceiling
# --------------------------------------------------------------------------
def test_a_change_that_would_burst_the_pack_is_refused_not_trimmed(cur, write):
    """Trimming to titles drops exactly the standing rules the session needed.

    A fixed cost that quietly stops delivering is worse than one that says it
    is full, and at the moment of the change there is still someone to hand it
    back to.
    """
    memory_id, _ = write(MemoryType.PREFERENCE, "an enormous rule", "long prose " * 3000)
    with pytest.raises(DeliveryError, match="over the ceiling"):
        store.set_delivery(
            cur, memory_id=memory_id, delivery=Delivery.STARTUP_REQUIRED, actor="user"
        )
    assert store.get_entity(cur, memory_id)["delivery"] == str(Delivery.PULL_ONLY)


def test_a_directive_short_enough_gets_in_where_the_content_would_not(cur, write):
    memory_id, _ = write(
        MemoryType.PREFERENCE,
        "an enormous rule with a short form",
        "long prose " * 3000,
        directive="keep it short",
    )
    store.set_delivery(
        cur, memory_id=memory_id, delivery=Delivery.STARTUP_REQUIRED, actor="user"
    )
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.startup[0]["content"] == "keep it short"
    assert got.over_budget is False


def test_an_index_that_alone_exceeds_the_ceiling_says_so(cur):
    """21.2 never trims the index, so past a certain ledger there is no way down."""
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
    assert got.trimmed == []


def test_the_scoped_material_gives_up_its_body_before_the_startup_pack(cur, scope_id, push, write):
    push(MemoryType.PREFERENCE, "reply language", "answer in Japanese " * 40)
    state, _ = write(MemoryType.STATE, "where the analysis stands", "the third run " * 40)

    got = bootstrap.session_bootstrap(cur, actor="claude", scopes=[scope_id], budget=250)
    assert state in got.trimmed
    assert all(row["memory_id"] not in got.trimmed for row in got.startup)


def test_a_payload_inside_the_budget_is_left_alone(cur, push):
    push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.trimmed == []
    assert got.over_budget is False


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------
def test_what_was_handed_over_is_logged(cur, push):
    memory_id = push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    got = bootstrap.session_bootstrap(cur, actor="claude")

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = %s",
        (str(EventType.SESSION_BOOTSTRAPPED),),
    )
    detail = cur.fetchone()["detail"]
    assert detail["memories"] == [str(memory_id)]
    assert detail["tokens"] == got.tokens


def test_setting_delivery_is_recorded(cur, push):
    memory_id = push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = %s AND memory_id = %s",
        (str(EventType.DELIVERY_SET), memory_id),
    )
    detail = cur.fetchone()["detail"]
    assert detail["from"] == str(Delivery.PULL_ONLY)
    assert detail["to"] == str(Delivery.STARTUP_REQUIRED)


def test_bootstrap_never_reaches_for_the_model(cur, push, monkeypatch):
    """There is no query to encode, and session start is where a cold load hurts."""
    import mashu.embed

    push(MemoryType.PREFERENCE, "reply language", "answer in Japanese")

    def refuse():
        raise AssertionError("bootstrap must not need an embedder")

    monkeypatch.setattr(mashu.embed, "get_embedder", refuse)
    assert bootstrap.session_bootstrap(cur, actor="claude").startup
