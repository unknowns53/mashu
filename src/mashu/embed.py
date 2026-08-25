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
import pathlib
import re
import sqlite3
import threading
from array import array
from typing import Protocol, runtime_checkable

DIMENSIONS = 1024
MODEL_ENV_VAR = "MASHU_EMBEDDING_MODEL"
DEFAULT_MODEL = "intfloat/multilingual-e5-large"
CACHE_ENV_VAR = "MASHU_EMBEDDING_CACHE"
DEFAULT_CACHE_DIR = "~/.cache/mashu"


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

    Loading costs seconds and a gigabyte of memory, so it is deferred until
    something actually asks for a vector. A process that only reads existing
    embeddings never pays for it.

    Loading is attempted from the local cache first. Left to itself the library
    contacts the model hub on every construction to check for a newer revision,
    which measured at 5.7 of the 7.2 seconds a load took, for a model already
    sitting on disk. The network path is kept as the fallback, because the very
    first run has nothing cached to load from.
    """

    dimensions = DIMENSIONS

    def __init__(self, model_name: str | None = None) -> None:
        self.name = model_name or os.environ.get(MODEL_ENV_VAR) or DEFAULT_MODEL
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            try:
                self._model = SentenceTransformer(self.name, local_files_only=True)
            except Exception:
                # Nothing cached yet, or the cache is incomplete. Fetch it.
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
# not loading the model at all
# --------------------------------------------------------------------------
class CachedEmbedder:
    """Remembers vectors across processes so a repeat never loads the model.

    Embedding is deterministic for a given model and text, so the same query
    asked twice does not need the weights a second time. That matters more than
    the arithmetic it saves: the lookup happens before the inner embedder is
    touched, and the inner embedder is what imports torch and reads a gigabyte
    off disk. A run that hits the cache for everything it needs never pays any
    of that.

    The model name is part of the key. Vectors from two models are not
    comparable, and silently mixing them would degrade similarity in a way that
    shows up as slightly worse answers rather than as an error.
    """

    def __init__(self, inner: Embedder, path: str | None = None) -> None:
        self._inner = inner
        self._path = (
            pathlib.Path(path or os.environ.get(CACHE_ENV_VAR) or DEFAULT_CACHE_DIR).expanduser()
            / "embeddings.sqlite3"
        )
        self._db: sqlite3.Connection | None = None
        # The MCP server answers tool calls on a thread pool, so the connection
        # opened by the first call is used by later ones from other threads.
        # sqlite3 refuses that by default and the refusal surfaced as a failed
        # memory_search rather than as a slow one, because this layer sits in
        # front of the embedder rather than beside it.
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    def _connect(self) -> sqlite3.Connection | None:
        """Open the cache, or give up on it.

        A cache that cannot be opened is not an error worth stopping for: the
        inner embedder still answers, only slower. Failing hard here would turn
        a read-only home directory into an outage.
        """
        if self._db is None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(self._path, check_same_thread=False)
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS vector ("
                    "  key TEXT PRIMARY KEY, value BLOB NOT NULL)"
                )
                self._db.commit()
            except (OSError, sqlite3.Error):
                self._db = False  # type: ignore[assignment]
        return self._db or None

    @staticmethod
    def _narrow(vector: list[float]) -> list[float]:
        """Put a fresh vector through the precision the cache stores.

        pgvector's own type is single precision, so the extra digits are
        discarded on the way into the database regardless. Narrowing here as
        well is what keeps a cache hit and a cache miss from returning
        different numbers for the same text.
        """
        return list(array("f", vector))

    def _key(self, role: str, text: str) -> str:
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        return f"{self.name}\x00{role}\x00{digest}"

    def _get(self, role: str, text: str) -> list[float] | None:
        with self._lock:
            db = self._connect()
            if db is None:
                return None
            row = db.execute(
                "SELECT value FROM vector WHERE key = ?", (self._key(role, text),)
            ).fetchone()
        if row is None:
            return None
        return list(array("f", row[0]))

    def _put(self, role: str, text: str, vector: list[float]) -> None:
        with self._lock:
            db = self._connect()
            if db is None:
                return
            try:
                db.execute(
                    "INSERT OR REPLACE INTO vector (key, value) VALUES (?, ?)",
                    (self._key(role, text), array("f", vector).tobytes()),
                )
                db.commit()
            except sqlite3.Error:
                pass

    def embed_query(self, text: str) -> list[float]:
        cached = self._get("query", text)
        if cached is not None:
            return cached
        vector = self._narrow(self._inner.embed_query(text))
        self._put("query", text, vector)
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        found: dict[int, list[float]] = {}
        missing: list[int] = []
        for i, text in enumerate(texts):
            cached = self._get("passage", text)
            if cached is None:
                missing.append(i)
            else:
                found[i] = cached

        if missing:
            fresh = self._inner.embed_documents([texts[i] for i in missing])
            for i, vector in zip(missing, fresh, strict=True):
                found[i] = self._narrow(vector)
                self._put("passage", texts[i], found[i])
        return [found[i] for i in range(len(texts))]


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
        # The hashing embedder is cheaper than a cache lookup, so it is not
        # wrapped: caching it would only add a file to keep coherent.
        _current = (
            HashingEmbedder() if configured == "hashing" else CachedEmbedder(E5Embedder(configured))
        )
    return _current


def set_embedder(embedder: Embedder | None) -> None:
    """Replace the process embedder. Passing None restores the configured one."""
    global _current
    _current = embedder
