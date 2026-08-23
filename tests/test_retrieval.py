"""The three layers, and the caps on the middle one.

Specifications 21, 21.1 and, for the token budget, v0.8.
"""

from __future__ import annotations

import pytest

from mashu import proposals, retrieval, store
from mashu.models import MemoryType, ProposalOperation, SourceType, VersionStatus


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


def test_layer_two_never_outnumbers_layer_one(cur, scope_id, author):
    """The relative cap of 21.1: the tag needs untagged material beside it."""
    author("ramp one", "the ramp is half a degree per minute")
    for i in range(5):
        author(f"ramp candidate {i}", f"the ramp wandered on run {i}", adopt=False)

    got = retrieval.retrieve(cur, "ramp", actor="claude", scope_id=scope_id)
    assert len(got.active) == 1
    assert len(got.unreviewed) == 1
    assert got.dropped_unreviewed == 4


def test_with_nothing_active_the_unreviewed_layer_is_empty(cur, scope_id, author):
    """A context built entirely of unreviewed content is what the cap prevents."""
    author("ramp candidate", "the ramp wandered on the second run", adopt=False)

    got = retrieval.retrieve(cur, "ramp", actor="claude", scope_id=scope_id)
    assert got.active == []
    assert got.unreviewed == []
    assert got.dropped_unreviewed == 1


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


def test_a_rejected_candidate_shows_up_as_retired(cur, scope_id, author):
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
    assert got.retired[0]["reason"] == "that was the uncalibrated thermocouple"


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


def test_scope_detection_finds_the_right_ledger_entry(cur):
    polymer = store.create_scope(
        cur, name="cloud point measurement", actor="user", description="ramp rates and DLS"
    )
    other = store.create_scope(
        cur, name="SSD failure analysis", actor="user", description="enclosure timeouts"
    )
    for scope, title, content in [
        (polymer, "ramp rate", "the ramp is half a degree per minute"),
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

    got = retrieval.retrieve(cur, "cloud point measurement", actor="claude")
    assert got.scopes == [polymer]
    assert _titles(got.active) == ["ramp rate"]


def test_an_undetectable_query_searches_everything_rather_than_guessing(cur, scope_id, author):
    """A wrong single guess hides the answer; a wide search only dilutes it."""
    author("ramp rate", "the ramp is half a degree per minute")
    got = retrieval.retrieve(cur, "zzzz unrelated", actor="claude")
    assert got.scopes == []
    assert _titles(got.active) == ["ramp rate"]


def test_a_scope_with_no_vector_is_skipped_rather_than_breaking_detection(cur, author):
    """Scopes predate migration 0006; backfill is what fills them in."""
    from mashu import store as _store

    detectable = _store.create_scope(
        cur, name="cloud point measurement", actor="user", description="ramp rates"
    )
    stale = _store.create_scope(cur, name="an older scope", actor="user")
    cur.execute("UPDATE scope SET name_embedding = NULL WHERE scope_id = %s", (stale,))

    _store.create_entity(
        cur,
        scope_id=detectable,
        type=MemoryType.FACT,
        title="ramp rate",
        content="the ramp is half a degree per minute",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
        adopt=True,
    )

    got = retrieval.retrieve(cur, "cloud point measurement", actor="claude")
    assert got.scopes == [detectable]


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
