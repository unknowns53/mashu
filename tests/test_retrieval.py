"""The three layers, and the caps on the middle one.

Specifications 21, 21.1 and, for the token budget, v0.8.
"""

from __future__ import annotations

import pytest

from mashu import proposals, retrieval, store
from mashu.models import (
    EntityStatus,
    EventType,
    MemoryType,
    ProposalOperation,
    SourceType,
    VersionStatus,
)


@pytest.fixture
def author(cur, scope_id):
    def _author(title, content, type=MemoryType.FACT, adopt=True):
        return store.create_entity(
            cur,
            scope_id=scope_id,
            type=type,
            title=title,
            content=content,
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=adopt,
        )

    return _author


def _titles(rows):
    return [row["title"] for row in rows]


# --------------------------------------------------------------------------
# layer 1
# --------------------------------------------------------------------------
def test_the_current_version_comes_back_with_its_content(cur, scope_id, author):
    author("cloud point ramp", "the cloud point ramp is half a degree per minute")
    author("force field", "the polymer runs on OPLS-AA")

    got = retrieval.retrieve(cur, "cloud point ramp rate", actor="claude", scope_id=scope_id)
    assert _titles(got.active)[0] == "cloud point ramp"
    assert "half a degree per minute" in got.active[0]["content"]


def test_a_superseded_wording_never_reaches_any_layer(cur, scope_id, author):
    """Scenario 2: correcting a memory has to retire the old text completely."""
    memory_id, first = author("cloud point ramp", "the cloud point ramp is one degree per minute")
    store.add_version(
        cur,
        memory_id=memory_id,
        content="the cloud point ramp is half a degree per minute",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        based_on_version=first,
        adopt=True,
    )

    got = retrieval.retrieve(cur, "cloud point ramp rate", actor="claude", scope_id=scope_id)
    everything = " ".join(
        row.get("content", "") + row.get("title", "")
        for row in (*got.active, *got.unreviewed, *got.retired)
    )
    assert "one degree per minute" not in everything
    assert "half a degree per minute" in everything


def test_the_type_filter_narrows_the_answer(cur, scope_id, author):
    author("cloud point ramp", "the ramp is half a degree per minute", type=MemoryType.FACT)
    author("ramp guess", "the ramp may not matter", type=MemoryType.HYPOTHESIS)

    got = retrieval.retrieve(
        cur, "ramp", actor="claude", scope_id=scope_id, types=[MemoryType.HYPOTHESIS]
    )
    assert _titles(got.active) == ["ramp guess"]


# --------------------------------------------------------------------------
# layer 2
# --------------------------------------------------------------------------
def test_an_unreviewed_candidate_arrives_tagged_but_whole(cur, scope_id, author):
    """v0.7 semi-approval: review confirms quality, it does not unlock use."""
    author("cloud point ramp", "the ramp is half a degree per minute")
    proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.OBSERVATION),
            "title": "ramp overshoot",
            "content": "the ramp overshot by two degrees on the second run",
            "source_type": str(SourceType.AGENT),
        },
        allow_similar=True,
    )

    got = retrieval.retrieve(cur, "cloud point ramp", actor="claude", scope_id=scope_id)
    assert _titles(got.unreviewed) == ["ramp overshoot"]
    assert "overshot by two degrees" in got.unreviewed[0]["content"]
    assert got.unreviewed[0]["tag"] == retrieval.UNREVIEWED_TAG


def test_an_adopted_version_is_not_also_unreviewed(cur, scope_id, author):
    author("cloud point ramp", "the ramp is half a degree per minute")
    got = retrieval.retrieve(cur, "cloud point ramp", actor="claude", scope_id=scope_id)
    assert got.unreviewed == []


def test_the_unreviewed_layer_may_outnumber_the_reviewed_one(cur, scope_id, author):
    """v0.11 withdrew the relative cap of 21.1.

    It emptied layer 2 in proportion to layer 1, so a scope with little adopted
    answered with little, and one with nothing adopted answered with nothing.
    That is review granting use, which v0.7 said it had stopped doing.
    """
    author("ramp one", "the ramp is half a degree per minute")
    for i in range(5):
        author(f"ramp candidate {i}", f"the ramp wandered on run {i}", adopt=False)

    got = retrieval.retrieve(cur, "ramp", actor="claude", scope_id=scope_id)
    assert len(got.active) == 1
    assert len(got.unreviewed) == 5
    assert got.dropped_unreviewed == 0


def test_a_scope_with_nothing_adopted_still_answers(cur, scope_id, author):
    """The case the withdrawal was for: everything imported, nothing reviewed yet."""
    author("ramp candidate", "the ramp wandered on the second run", adopt=False)

    got = retrieval.retrieve(cur, "ramp", actor="claude", scope_id=scope_id)
    assert got.active == []
    assert [row["title"] for row in got.unreviewed] == ["ramp candidate"]
    assert got.unreviewed[0]["tag"] == retrieval.UNREVIEWED_TAG
    assert got.dropped_unreviewed == 0


def test_the_token_budget_stops_a_long_candidate(cur, scope_id, author, monkeypatch):
    monkeypatch.setattr(retrieval, "LAYER2_TOKEN_BUDGET", 20)
    author("ramp one", "the ramp is half a degree per minute")
    author("ramp two", "the ramp is stable")
    author("ramp long", "ramp " + ("wandered " * 200), adopt=False)
    author("ramp short", "ramp wandered", adopt=False)

    got = retrieval.retrieve(cur, "ramp", actor="claude", scope_id=scope_id)
    assert len(got.unreviewed) == 1
    assert got.dropped_unreviewed == 1


def test_estimate_tokens_counts_cjk_more_heavily():
    assert retrieval.estimate_tokens("曇点測定") == 4
    assert retrieval.estimate_tokens("cloud point") == 3


# --------------------------------------------------------------------------
# layer 3
# --------------------------------------------------------------------------
def test_a_disproven_memory_returns_its_reason_and_not_its_content(cur, scope_id, author):
    """Scenario 1, day 10. The refutation is what stops the rederivation."""
    memory_id, version_id = author(
        "SSD timeout cause",
        "the timeout comes from the enclosure bridge chip",
        type=MemoryType.HYPOTHESIS,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="the timeout reproduced over a direct SATA connection",
    )

    got = retrieval.retrieve(cur, "SSD timeout cause", actor="claude", scope_id=scope_id)
    assert [row["memory_id"] for row in got.retired] == [memory_id]
    retired = got.retired[0]
    assert retired["status"] == VersionStatus.DISPROVEN
    assert retired["reason"] == "the timeout reproduced over a direct SATA connection"
    assert "content" not in retired
    assert all("bridge chip" not in row["content"] for row in got.active)


def test_a_rejected_candidate_shows_up_as_retired_and_leaves_layer_two(cur, scope_id, author):
    """Turned down, so it is no longer unreviewed and no longer readable.

    Layer 2 selects on the version status alone. That only stays honest while
    a rejection actually moves the version off candidate, which is the reason
    rejected is a status and not a fact kept over in the proposal table.
    """
    result = proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.OBSERVATION),
            "title": "ramp overshoot",
            "content": "the ramp overshot by two degrees",
            "source_type": str(SourceType.AGENT),
        },
    )
    proposals.reject(
        cur,
        result["proposal"]["proposal_id"],
        reviewer="user",
        reason="that was the uncalibrated thermocouple",
    )

    got = retrieval.retrieve(cur, "ramp overshoot", actor="claude", scope_id=scope_id)
    assert _titles(got.retired) == ["ramp overshoot"]
    retired = got.retired[0]
    assert retired["status"] == VersionStatus.REJECTED
    assert retired["reason"] == "that was the uncalibrated thermocouple"
    assert "content" not in retired
    assert _titles(got.unreviewed) == []


def test_a_scope_with_nothing_left_active_still_answers_from_layer_three(cur, scope_id, author):
    """Day 10 of scenario 1, without the scope handed in.

    Layers 2 and 3 are confined to the scopes layer 1 hit, which works while
    something is active to confine them to. Once every memory in the scope has
    been retired there is nothing to confine by, and treating that as "confine
    to nothing" silenced the refutation in the one case it was written for.
    """
    memory_id, version_id = author(
        "SSD timeout cause",
        "the timeout comes from the enclosure bridge chip",
        type=MemoryType.HYPOTHESIS,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="the timeout reproduced over a direct SATA connection",
    )

    got = retrieval.retrieve(cur, "why does the SSD time out", actor="claude")

    assert got.active == []
    assert [row["memory_id"] for row in got.retired] == [memory_id]
    assert "direct SATA connection" in got.retired[0]["reason"]


def test_preview_shows_the_ranking_the_caps_hide(cur, scope_id, author, monkeypatch):
    """27.1 asks whether the query finds the right memory. Retrieval cannot say.

    Since v0.11 a freshly migrated scope does answer, but the token cap still
    decides how far down the list the answer reaches, and the ranking below
    that line is exactly what the migration has to check. Preview reads it, and
    stays out of context to do so.
    """
    monkeypatch.setattr(retrieval, "LAYER2_TOKEN_BUDGET", 20)
    for i in range(4):
        author("SSD timeout cause", f"reading {i} of the enclosure timing out", adopt=False)

    served = retrieval.retrieve(cur, "why does the SSD time out", actor="claude", scope_id=scope_id)
    assert served.active == []
    assert len(served.unreviewed) < 4

    ranked = retrieval.preview(cur, "why does the SSD time out", scope_id=scope_id)
    assert len(ranked) == 4
    assert [row["similarity"] for row in ranked] == sorted(
        (row["similarity"] for row in ranked), reverse=True
    )


def test_preview_is_not_recorded_as_context(cur, scope_id, author):
    """Nothing was assembled, so nothing is logged as having been handed over."""
    author("SSD timeout cause", "the enclosure bridge chip", adopt=False)
    retrieval.preview(cur, "why does the SSD time out", scope_id=scope_id)

    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = %s",
        (str(EventType.CONTEXT_ASSEMBLED),),
    )
    assert cur.fetchone()["n"] == 0


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
def test_every_layer_is_written_to_the_event_log(cur, scope_id, author):
    """Specification 22: what was handed over has to be reconstructable."""
    memory_id, version_id = author("SSD timeout cause", "the bridge chip times out")
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.DISPROVEN,
        actor="user",
        reason="reproduced over direct SATA",
    )
    author("SSD timeout measurement", "the timeout is nine seconds")

    got = retrieval.retrieve(cur, "SSD timeout", actor="claude", scope_id=scope_id)

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'context_assembled' ORDER BY event_id DESC"
    )
    detail = cur.fetchone()["detail"]
    assert detail["query"] == "SSD timeout"
    assert detail["layer1"] == [str(r["memory_id"]) for r in got.active]
    assert detail["layer3"] == [str(r["memory_id"]) for r in got.retired]
    assert str(memory_id) in detail["layer3"]


def test_scope_detection_reads_what_a_scope_holds_not_what_it_is_called(cur):
    """27.2 measured the label method and it does not separate.

    Matching a query against a scope's name and one-line summary put every real
    query inside a tenth of every scope and got the ranking wrong four times in
    seven. Three words cannot say what a scope is about. What a scope holds can,
    so the query goes against the memories and the scopes the best matches live
    in are the answer — a question about concentration rather than about an
    absolute similarity.
    """
    polymer = store.create_scope(cur, name="scope one", actor="user", description="unrelated words")
    other = store.create_scope(cur, name="scope two", actor="user", description="unrelated words")
    for scope, title, content in [
        (polymer, "ramp rate", "the ramp is half a degree per minute"),
        (polymer, "ramp check", "the ramp is checked before every run"),
        (polymer, "ramp limit", "the ramp is never above one degree per minute"),
        (other, "timeout length", "the timeout is nine seconds"),
    ]:
        store.create_entity(
            cur,
            scope_id=scope,
            type=MemoryType.FACT,
            title=title,
            content=content,
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=True,
        )

    # The scope names say nothing about ramps; only the memories do.
    got = retrieval.retrieve(cur, "the ramp is half a degree per minute", actor="claude")
    assert got.scopes == [polymer]
    assert "ramp rate" in _titles(got.active)


def test_a_scope_holding_only_candidates_is_reachable_without_being_named(cur):
    """The withdrawal of v0.11, reaching the path that decides where to look.

    A scope whose every memory is still a candidate holds nothing adopted, so
    it cannot appear in the probe if the probe reads adopted versions only, and
    it cannot appear among layer 1's rows either. Confining layers 2 and 3 to
    layer 1's scopes then put review back in front of use: on the real ledger
    one scope of four was in this state and none of its 37 memories could be
    retrieved by any query. So the probe reads what the layers hand over, and
    the fallback stands on the probe rather than on layer 1.

    The scope is deliberately not handed in. Passing scope_id is what the
    existing coverage does, and it is the one route that never had the problem.
    """
    adopted = store.create_scope(cur, name="scope one", actor="user")
    unreviewed = store.create_scope(cur, name="scope two", actor="user")
    store.create_entity(
        cur,
        scope_id=adopted,
        type=MemoryType.FACT,
        title="timeout length",
        content="the timeout is nine seconds",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )
    for i in range(3):
        store.create_entity(
            cur,
            scope_id=unreviewed,
            type=MemoryType.FACT,
            title=f"ramp note {i}",
            content=f"the ramp is half a degree per minute on run {i}",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=False,
        )

    got = retrieval.retrieve(cur, "the ramp is half a degree per minute", actor="claude")
    assert got.active == []
    assert _titles(got.unreviewed)
    assert all(title.startswith("ramp note") for title in _titles(got.unreviewed))
    assert unreviewed in got.narrowed_to


def test_the_probe_reads_candidates_as_well_as_adopted_versions(cur):
    """Detection is about what a scope holds, and it holds its candidates too."""
    from mashu.embed import get_embedder

    quiet = store.create_scope(cur, name="scope one", actor="user")
    busy = store.create_scope(cur, name="scope two", actor="user")
    store.create_entity(
        cur,
        scope_id=quiet,
        type=MemoryType.FACT,
        title="timeout length",
        content="the timeout is nine seconds",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )
    for i in range(3):
        store.create_entity(
            cur,
            scope_id=busy,
            type=MemoryType.FACT,
            title=f"ramp note {i}",
            content=f"the ramp is half a degree per minute on run {i}",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=False,
        )

    vector = retrieval._as_vector(
        get_embedder().embed_query("the ramp is half a degree per minute")
    )
    reading = retrieval.detect_scopes(cur, vector)
    assert busy in reading.probed


def test_an_undetectable_query_searches_everything_rather_than_guessing(cur, scope_id, author):
    """A wrong single guess hides the answer; a wide search only dilutes it."""
    author("ramp rate", "the ramp is half a degree per minute")
    got = retrieval.retrieve(cur, "zzzz unrelated", actor="claude")
    assert got.scopes == []
    assert _titles(got.active) == ["ramp rate"]


def test_the_only_scope_in_the_ledger_is_never_narrowed_to(cur):
    """A scope that is the whole store answers no better than its size predicts.

    Concentration is lift, so a scope holding everything cannot clear it however
    well it matches, and detection declines. This is the mechanism working: an
    empty answer means "search everything", which is where a query against a
    one-scope ledger was going anyway. The ceiling this expresses is 1 / lift —
    a scope past 83% of the ledger stops being detectable, and narrowing to a
    scope that large buys nothing.
    """
    small = store.create_scope(cur, name="scope one", actor="user")
    store.create_entity(
        cur,
        scope_id=small,
        type=MemoryType.FACT,
        title="ramp rate",
        content="the ramp is half a degree per minute",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )

    got = retrieval.retrieve(cur, "the ramp is half a degree per minute", actor="claude")
    assert got.scopes == []
    assert _titles(got.active) == ["ramp rate"]


def test_the_ledger_is_not_re_encoded_on_every_query(cur, scope_id, author, monkeypatch):
    """Scope names change when the user adds a scope, not once per search."""
    from mashu import embed

    author("ramp rate", "the ramp is half a degree per minute")
    calls: list[list[str]] = []
    inner = embed.get_embedder()

    class Watched:
        name = inner.name
        dimensions = inner.dimensions

        def embed_query(self, text):
            return inner.embed_query(text)

        def embed_documents(self, texts):
            calls.append(list(texts))
            return inner.embed_documents(texts)

    embed.set_embedder(Watched())
    retrieval.retrieve(cur, "ramp rate", actor="claude")
    assert calls == []


# --------------------------------------------------------------------------
# the whole active set, which retirement detection reads instead of querying
# --------------------------------------------------------------------------
def test_the_active_set_returns_everything_the_scope_holds_as_true(cur, scope_id, author):
    """No ranking, no limit: what is not asked for is what goes on being wrong."""
    for n in range(30):
        author(f"memory {n:02d}", f"the body of memory {n:02d}")

    rows = retrieval.active_set(cur, scope_id=scope_id)
    assert len(rows) == 30
    assert all(row["content"] for row in rows)


def test_the_active_set_leaves_out_what_has_no_current_truth(cur, scope_id, author):
    author("adopted", "this one stands")
    author("still waiting", "this one has not been reviewed", adopt=False)

    assert _titles(retrieval.active_set(cur, scope_id=scope_id)) == ["adopted"]


def test_a_provisional_entity_is_not_yet_part_of_what_the_scope_holds(cur, scope_id, author):
    """Section 20.1: it is still waiting to be told apart from another entity."""
    memory_id, _ = author("under a second name", "the same investigation")
    store.set_entity_status(
        cur,
        memory_id=memory_id,
        target=EntityStatus.PROVISIONAL,
        actor="user",
        reason="a title similarity above the threshold",
    )
    assert retrieval.active_set(cur, scope_id=scope_id) == []


def test_the_active_set_is_not_logged_as_context(cur, scope_id, author):
    """It is an input to extraction, not something an agent was handed."""
    author("a memory", "a body")
    retrieval.active_set(cur, scope_id=scope_id)

    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = %s",
        (str(EventType.CONTEXT_ASSEMBLED),),
    )
    assert cur.fetchone()["n"] == 0


def test_a_completed_version_still_active_says_so(cur, scope_id, author):
    """A completion keeps the pointer, so layer 1 has to carry the completion too.

    'this was done' is worth finding, which is why store keeps the active
    pointer on a completed version. What it must not do is arrive looking like
    work still outstanding: the store held the completion, said so in the
    version's own reason, and handed the reader the opposite.
    """
    memory_id, version_id = author(
        "台帳化フックが未実装",
        "セッション終了時に抽出を起こす入口が無い。",
        type=MemoryType.TASK,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.COMPLETED,
        actor="user",
        reason="launchd 経由で実際に抽出が成功した",
    )

    got = retrieval.retrieve(cur, "台帳化フックが未実装", actor="claude", record=False)
    row = next(r for r in got.active if r["memory_id"] == memory_id)
    assert row["tag"] == retrieval.FINISHED_TAG
    assert row["version_status"] == str(VersionStatus.COMPLETED)
    assert "launchd" in row["version_reason"]


def test_an_ordinary_active_row_carries_no_such_mark(cur, scope_id, author):
    """A mark on everything is a mark on nothing."""
    memory_id, _ = author("送風の設定は測定から決める", "推測で置いた値は下流へ渡さない。")
    got = retrieval.retrieve(cur, "送風の設定は測定から決める", actor="claude", record=False)
    row = next(r for r in got.active if r["memory_id"] == memory_id)
    assert row.get("tag") is None


def test_being_finished_outranks_being_proposed_for_retirement(cur, scope_id, author):
    """A version that already carries a retirement is not a question anyone is asking."""
    memory_id, version_id = author("もう片付いた作業", "本文はここにある。", type=MemoryType.TASK)
    proposals.propose(
        cur,
        actor="claude",
        operation=ProposalOperation.CHANGE_STATUS,
        payload={
            "version_id": str(version_id),
            "status": str(VersionStatus.DORMANT),
            "reason": "しばらく触らない",
            "source_type": str(SourceType.AGENT),
        },
        target_memory=memory_id,
    )
    store.set_status(
        cur,
        version_id=version_id,
        target=VersionStatus.COMPLETED,
        actor="user",
        reason="終わった",
    )

    got = retrieval.retrieve(cur, "もう片付いた作業", actor="claude", record=False)
    row = next(r for r in got.active if r["memory_id"] == memory_id)
    assert row["tag"] == retrieval.FINISHED_TAG
