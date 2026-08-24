"""Session Bootstrap (specification 21.2)."""

from __future__ import annotations

import pytest

from mashu import bootstrap, proposals, store
from mashu.errors import DeliveryError, NotFoundError
from mashu.models import Delivery, EventType, MemoryType, ProposalOperation, SourceType


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


def test_the_map_carries_only_what_names_a_scope(cur, scope_id):
    """v0.11 took the lifecycle off the map.

    It was there so an empty answer from an unreviewed scope would not read as
    "nothing is known about this". Such a scope now answers with tagged
    candidates, so the label had nothing left to disambiguate, and a label
    nothing maintains is read as current long after it stops being true.
    """
    got = bootstrap.session_bootstrap(cur, actor="claude")
    entry = next(row for row in got.scope_index if row["scope_id"] == scope_id)
    assert set(entry) == {"scope_id", "name", "summary"}


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
    store.set_delivery(cur, memory_id=memory_id, delivery=Delivery.STARTUP_REQUIRED, actor="user")
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


# --------------------------------------------------------------------------
# the ceiling holds wherever a version is adopted, not only where it is pushed
# --------------------------------------------------------------------------
def test_a_current_state_that_would_burst_its_scope_pack_cannot_be_adopted(cur, scope_id, write):
    """A pushed memory bursts the pack by growing, not only by being promoted."""
    memory_id, first = write(MemoryType.STATE, "where things stand", "short enough")
    assert store.get_entity(cur, memory_id)["delivery"] == str(Delivery.SCOPE_REQUIRED)

    with pytest.raises(DeliveryError, match="over 2000"):
        store.add_version(
            cur,
            memory_id=memory_id,
            content="word " * 3000,
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            based_on_version=first,
            adopt=True,
        )

    assert store.get_entity(cur, memory_id)["active_version"] == first


def test_the_same_length_is_fine_when_nothing_pushes_it(cur, scope_id, write):
    """Only what a session is handed unasked is bounded; the rest is pulled."""
    memory_id, first = write(MemoryType.FACT, "a long fact", "short enough")
    assert store.get_entity(cur, memory_id)["delivery"] == str(Delivery.PULL_ONLY)

    store.add_version(
        cur,
        memory_id=memory_id,
        content="word " * 3000,
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        based_on_version=first,
        adopt=True,
    )
    assert store.get_entity(cur, memory_id)["active_version"] != first


def test_one_scope_state_does_not_count_against_another(cur, scope_id, write):
    """scope_required is measured against the pack a session in that scope gets."""
    other = store.create_scope(cur, name="another scope", actor="user")
    write(MemoryType.STATE, "the other scope stands here", "word " * 300, scope=other)

    memory_id, _ = write(MemoryType.STATE, "this scope stands here", "word " * 300)
    entity = store.get_entity(cur, memory_id)
    assert entity["active_version"] is not None

    fits, cost = bootstrap.would_fit(cur, scope_id=scope_id)
    assert fits, cost


# --------------------------------------------------------------------------
# writing the short form onto a memory that already exists (21.2)
# --------------------------------------------------------------------------
def test_a_directive_written_later_shortens_the_push_without_a_new_version(cur, push):
    """The short form is a compression of reviewed content, not a revision.

    Everything the importer brought in arrived without one, so before this the
    only way to shorten a push was to file a revision whose body had not
    changed — an identical content twice in the history, reading as a change of
    mind about the content (the objection section 8 raises about retyping).
    """
    long_body = "測ってから決める。" * 40
    memory_id = push(MemoryType.PREFERENCE, "測ってから決める", long_body)

    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    before = cur.fetchone()["n"]
    was_active = store.get_entity(cur, memory_id)["active_version"]
    full = bootstrap.session_bootstrap(cur, actor="claude").tokens

    store.set_directive(cur, memory_id=memory_id, directive="測ってから決める。", actor="user")

    cur.execute("SELECT count(*) AS n FROM memory_version WHERE memory_id = %s", (memory_id,))
    assert cur.fetchone()["n"] == before
    assert store.get_entity(cur, memory_id)["active_version"] == was_active

    got = bootstrap.session_bootstrap(cur, actor="claude")
    assert got.tokens < full
    assert got.startup[0]["content"] == "測ってから決める。"

    # The whole of it is still there for anything that pulls.
    assert store.get_version(cur, was_active)["content"] == long_body


def test_a_directive_that_bursts_the_pack_is_handed_back(cur, push):
    """21.2 refuses the change rather than trimming the pack to fit."""
    memory_id = push(MemoryType.PREFERENCE, "短く始める", "短く始める。")
    with pytest.raises(DeliveryError):
        store.set_directive(cur, memory_id=memory_id, directive="長い規則。" * 500, actor="user")
    assert (
        store.get_version(cur, store.get_entity(cur, memory_id)["active_version"])["directive"]
        is None
    )


def test_a_memory_with_nothing_adopted_has_no_short_form_to_write(cur, write):
    """A directive is the short form of what the store currently holds as true."""
    memory_id, _ = write(MemoryType.PREFERENCE, "まだ採用されていない", "本文", adopt=False)
    with pytest.raises(NotFoundError):
        store.set_directive(cur, memory_id=memory_id, directive="短く", actor="user")


# --------------------------------------------------------------------------
# what a promotion is weighed against (21.2)
# --------------------------------------------------------------------------
def test_a_startup_promotion_is_weighed_against_the_heaviest_opening(cur, scope_id, write):
    """The ceiling bounds an opening, not the part of it everyone shares.

    A startup rule rides with every scope in turn, so weighing it against the
    startup pack alone accepts promotions that leave a scoped session opening
    without its current state — the state being the longest thing in any pack,
    and so the first thing the trimming drops.
    """
    state_id, _ = write(MemoryType.STATE, "現在地", "現在地。" * 470)
    store.set_delivery(cur, memory_id=state_id, delivery=Delivery.SCOPE_REQUIRED, actor="user")

    rule_id, _ = write(MemoryType.PREFERENCE, "規律", "規律。" * 270)
    with pytest.raises(DeliveryError):
        store.set_delivery(cur, memory_id=rule_id, delivery=Delivery.STARTUP_REQUIRED, actor="user")
    assert store.get_entity(cur, rule_id)["delivery"] == Delivery.PULL_ONLY

    # It is only the loaded scope that refuses it. A session opening in a scope
    # that pushes nothing had room all along, which is exactly why measuring
    # that opening was no answer.
    empty = store.create_scope(cur, name="a scope that pushes nothing", actor="user")
    assert bootstrap.would_fit(cur, memory_id=rule_id, content="規律。" * 270, scope_id=empty)[0]
    assert not bootstrap.would_fit(cur, memory_id=rule_id, content="規律。" * 270)[0]


def test_a_scope_promotion_is_weighed_at_all(cur, scope_id, write):
    """Joining a scope's pack went unmeasured, so only startup was ever refused.

    A state arrives scope_required and so was weighed when it was adopted, but
    anything else reaching that pack got there by this call, which measured
    nothing at all.
    """
    memory_id, _ = write(MemoryType.PREFERENCE, "長い規律", "規律。" * 950)
    with pytest.raises(DeliveryError):
        store.set_delivery(cur, memory_id=memory_id, delivery=Delivery.SCOPE_REQUIRED, actor="user")
    assert store.get_entity(cur, memory_id)["delivery"] == Delivery.PULL_ONLY


def test_the_push_says_when_a_newer_version_is_waiting(cur, scope_id):
    """21.1's other half: the annotation existed only for retirement.

    What is pushed is the adopted reading. A Current State goes out of date in
    hours and a review takes days, so the session most in need of knowing that
    a newer reading is written is the one being handed the older one before it
    has asked anything.
    """
    memory_id, version_id = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.STATE,
        title="where the work stands",
        content="the first scenario is shipped",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )
    before = bootstrap.session_bootstrap(cur, actor="claude", scopes=[scope_id])
    assert not any(row.get("update_proposed_by") for row in before.scoped)

    proposals.propose(
        cur,
        actor="mashu-worker",
        operation=ProposalOperation.UPDATE_VERSION,
        target_memory=memory_id,
        based_on_version=version_id,
        payload={
            "content": "the second scenario is shipped too",
            "source_type": str(SourceType.AGENT),
        },
    )

    after = bootstrap.session_bootstrap(cur, actor="claude", scopes=[scope_id])
    pushed = [row for row in after.scoped if row["memory_id"] == memory_id]
    assert len(pushed) == 1
    assert pushed[0]["update_proposed_by"] == "mashu-worker"
    # Still the adopted reading. The note says a newer one exists; it does not
    # hand it over, which is what layer 2 is for.
    assert "first scenario" in pushed[0]["content"]


def test_the_refusal_names_a_route_that_exists(cur, scope_id, write):
    """The advice sent the reader to a door that is shut from this side.

    set_directive writes onto the adopted version, so a candidate waiting to be
    adopted cannot be given one: adopting is the thing being refused. Telling
    its holder to give it a directive costs them the round trip of finding that
    out, and the way through — proposing it again carrying one — went unsaid.
    """
    memory_id, _ = write(MemoryType.STATE, "where things stand", "short enough")

    with pytest.raises(DeliveryError, match="propose it again carrying one"):
        store.add_version(
            cur,
            memory_id=memory_id,
            content="word " * 3000,
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            adopt=True,
            based_on_version=store.get_entity(cur, memory_id)["latest_version"],
        )

    # With one, the advice is about shortening what is there.
    with pytest.raises(DeliveryError, match="shorten its directive"):
        store.add_version(
            cur,
            memory_id=memory_id,
            content="word " * 3000,
            directive="word " * 3000,
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            adopt=True,
            based_on_version=store.get_entity(cur, memory_id)["latest_version"],
        )
