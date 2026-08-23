"""Turning text into the vectors retrieval searches on.

Two things need embedding and they are not the same job. A title is embedded to
ask whether an entity already exists (specification 20); content is embedded to
ask whether a memory answers a query (21). Both live in the same vector space
here, but they are looked up against different columns and different
thresholds.

The model is multilingual-e5-large, which expects its inputs prefixed by role:
a stored passage and the question asked of it are encoded differently, and
dropping the prefixes quietly costs accuracy rather than failing. Those
prefixes are applied here so no caller has to remember them.

A hashing embedder stands in when the model is not available. It is a real
embedder, not a stub: character trigrams hashed into the same 1024 dimensions
and normalised, so lexically similar strings genuinely come out close. That is
enough to exercise the pipeline in tests without a two gigabyte download, and
not enough to stand in for the model in the threshold measurement of 27.2,
which is why the measurement names the model it ran against.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, runtime_checkable

DIMENSIONS = 1024
MODEL_ENV_VAR = "MASHU_EMBEDDING_MODEL"
DEFAULT_MODEL = "intfloat/multilingual-e5-large"


@runtime_checkable
class Embedder(Protocol):
    """What retrieval needs from whatever produces vectors."""

    name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Vectors for text being stored."""

    def embed_query(self, text: str) -> list[float]:
        """A vector for text being searched with."""


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------
class E5Embedder:
    """multilingual-e5-large, loaded on first use.

    Loading costs seconds and hundreds of megabytes of memory, so it is
    deferred until something actually asks for a vector. A process that only
    reads existing embeddings never pays for it.
    """

    dimensions = DIMENSIONS

    def __init__(self, model_name: str | None = None) -> None:
        self.name = model_name or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name)
            getter = (
                getattr(self._model, "get_embedding_dimension", None)
                or self._model.get_sentence_embedding_dimension
            )
            got = getter()
            if got != DIMENSIONS:
                raise ValueError(
                    f"{self.name} produces {got} dimensions, but the schema stores "
                    f"{DIMENSIONS}; changing models means a migration that rebuilds "
                    f"the embedding columns"
                )
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return [list(map(float, row)) for row in model.encode(texts, normalize_embeddings=True)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([f"passage: {t}" for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([f"query: {text}"])[0]


# --------------------------------------------------------------------------
# the stand-in
# --------------------------------------------------------------------------
_WORD = re.compile(r"\w+", re.UNICODE)


class HashingEmbedder:
    """Character trigrams hashed into the vector space, then normalised.

    Deterministic across processes, which matters because a stored embedding
    and a query embedding are compared long after the process that wrote one of
    them has gone. Python's own hash is salted per process, so it cannot be
    used here.
    """

    name = "hashing"
    dimensions = DIMENSIONS

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * DIMENSIONS
        for token in _WORD.findall(text.lower()):
            padded = f"^{token}$"
            for i in range(len(padded) - 2):
                digest = hashlib.blake2b(padded[i : i + 3].encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % DIMENSIONS
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


# --------------------------------------------------------------------------
# choosing one
# --------------------------------------------------------------------------
_current: Embedder | None = None


def get_embedder() -> Embedder:
    """The embedder this process uses, built once.

    MASHU_EMBEDDING_MODEL set to "hashing" selects the stand-in. Anything else
    names a sentence-transformers model.
    """
    global _current
    if _current is None:
        configured = os.environ.get(MODEL_ENV_VAR, DEFAULT_MODEL)
        _current = HashingEmbedder() if configured == "hashing" else E5Embedder(configured)
    return _current


def set_embedder(embedder: Embedder | None) -> None:
    """Replace the process embedder. Passing None restores the configured one."""
    global _current
    _current = embedder
