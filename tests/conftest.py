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
