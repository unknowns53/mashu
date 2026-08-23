"""Test fixtures.

The suite runs against a real PostgreSQL, not a substitute. Most of what this
layer promises lives in constraints, a trigger and transaction boundaries, so a
fake database would test the wrong thing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from mashu.embed import HashingEmbedder, set_embedder
from mashu.migrate import migrate

TEST_DB = os.environ.get("MASHU_TEST_DB", "mashu_test")
ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")


@pytest.fixture(scope="session")
def test_dsn() -> str:
    """A database built from the migrations, rebuilt once per run."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')

    dsn = f"dbname={TEST_DB}"
    applied = migrate(dsn)
    assert applied, "no migrations were applied to the test database"
    return dsn


@pytest.fixture(scope="session")
def committing_dsn() -> str:
    """A second database for tests that have to commit.

    The CLI opens its own connection, so its tests cannot be wrapped in a
    transaction that is rolled back afterwards. Their rows therefore persist,
    and sharing a database with the rolled-back tests would let committed rows
    turn up in queries those tests expect to see only their own fixtures in.
    Separating them keeps both kinds honest rather than making one defensive.
    """
    name = f"{TEST_DB}_committing"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = f"dbname={name}"
    migrate(dsn)
    return dsn


@pytest.fixture
def cur(test_dsn: str) -> Iterator[psycopg.Cursor]:
    """A cursor whose transaction is rolled back when the test ends.

    Rolling back is also the only way to clear event_log, since the trigger
    refuses DELETE and TRUNCATE.
    """
    conn = psycopg.connect(test_dsn, row_factory=dict_row)
    try:
        with conn.cursor() as cursor:
            yield cursor
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def scope_id(cur: psycopg.Cursor):
    """A scope to hang test entities on."""
    from mashu.store import create_scope

    return create_scope(cur, name="test scope", actor="test")


@pytest.fixture(autouse=True)
def hashing_embeddings() -> Iterator[None]:
    """Run the suite on the stand-in embedder rather than the model.

    The tests here are about the pipeline: that a query reaches the right
    layer, that the caps hold, that a near-duplicate title is offered before a
    second entity is made. None of that is a claim about the model, and 27.2 is
    where the model's own numbers get measured. Loading two gigabytes of
    weights to assert plumbing would slow every run for nothing.
    """
    set_embedder(HashingEmbedder())
    try:
        yield
    finally:
        set_embedder(None)
