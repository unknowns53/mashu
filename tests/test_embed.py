"""The embedding cache, and what it is for.

The point of the cache is not the arithmetic it saves. Encoding one short query
takes under a tenth of a second; loading the weights that do it takes several,
and a gigabyte of reading. So what matters is that a hit is answered without
the inner embedder being touched at all.
"""

from __future__ import annotations

import pytest

from mashu.embed import DIMENSIONS, CachedEmbedder, HashingEmbedder


class CountingEmbedder:
    """A stand-in that records how often it was actually asked to work."""

    dimensions = DIMENSIONS

    def __init__(self, name: str = "counting") -> None:
        self.name = name
        self._inner = HashingEmbedder()
        self.query_calls = 0
        self.document_calls: list[list[str]] = []

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._inner.embed_query(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return self._inner.embed_documents(texts)


@pytest.fixture
def cached(tmp_path):
    inner = CountingEmbedder()
    return CachedEmbedder(inner, path=str(tmp_path)), inner


def test_a_repeated_query_never_reaches_the_model(cached):
    embedder, inner = cached
    first = embedder.embed_query("なぜ SSD がタイムアウトするのか")
    second = embedder.embed_query("なぜ SSD がタイムアウトするのか")
    assert first == second
    assert inner.query_calls == 1


def test_a_different_query_is_not_served_from_the_cache(cached):
    embedder, inner = cached
    embedder.embed_query("最初の質問")
    embedder.embed_query("別の質問")
    assert inner.query_calls == 2


def test_only_the_uncached_documents_are_encoded(cached):
    embedder, inner = cached
    embedder.embed_documents(["あ", "い"])
    vectors = embedder.embed_documents(["あ", "う", "い"])

    assert inner.document_calls == [["あ", "い"], ["う"]]
    assert vectors[0] == embedder.embed_documents(["あ"])[0]
    assert len(vectors) == 3


def test_a_hit_and_a_miss_agree_exactly(cached):
    """Otherwise the same text would rank differently depending on cache state."""
    embedder, _ = cached
    fresh = embedder.embed_query("同じ問い")
    hit = embedder.embed_query("同じ問い")
    assert fresh == hit


def test_the_order_survives_a_partial_hit(cached):
    """A mixed batch must come back in the order it was asked for."""
    embedder, _ = cached
    warmed = embedder.embed_documents(["ひとつ", "ふたつ", "みっつ"])
    mixed = embedder.embed_documents(["みっつ", "よっつ", "ひとつ"])
    assert mixed[0] == warmed[2]
    assert mixed[2] == warmed[0]


def test_a_query_and_a_passage_are_kept_apart(cached):
    """e5 encodes the two roles differently; one must not answer for the other."""
    embedder, inner = cached
    embedder.embed_query("同じ文字列")
    embedder.embed_documents(["同じ文字列"])
    assert inner.query_calls == 1
    assert inner.document_calls == [["同じ文字列"]]


def test_two_models_do_not_share_vectors(tmp_path):
    """Mixing vectors from two models degrades similarity without erroring."""
    one = CountingEmbedder("model-one")
    two = CountingEmbedder("model-two")
    CachedEmbedder(one, path=str(tmp_path)).embed_query("質問")
    CachedEmbedder(two, path=str(tmp_path)).embed_query("質問")
    assert one.query_calls == 1
    assert two.query_calls == 1


def test_an_unusable_cache_does_not_stop_the_work(tmp_path):
    """A read-only home is slower, not broken."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    inner = CountingEmbedder()
    embedder = CachedEmbedder(inner, path=str(blocked))

    assert len(embedder.embed_query("質問")) == DIMENSIONS
    assert len(embedder.embed_query("質問")) == DIMENSIONS
    assert inner.query_calls == 2


def test_the_cache_survives_a_new_process(tmp_path):
    """Two CLI invocations are two processes; an in-memory cache would miss."""
    inner = CountingEmbedder()
    CachedEmbedder(inner, path=str(tmp_path)).embed_query("質問")

    later = CountingEmbedder()
    CachedEmbedder(later, path=str(tmp_path)).embed_query("質問")
    assert later.query_calls == 0
