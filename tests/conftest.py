"""Test fixtures.

The suite runs against a real PostgreSQL, not a substitute. Most of what this
layer promises lives in constraints, a trigger and transaction boundaries, so
a fake database would test the wrong thing.

There is no embedder fixture any more. v2 matches with pg_trgm, which the
database already has, so the strings below are chosen for what trigram
similarity does with them: near-identical text for "the same hole", text with
no shared substrings for "a different one".
"""

from __future__ import annotations

import os
import pathlib
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
    """A scope to hang test rows on."""
    from mashu.scopes import create_scope

    return create_scope(cur, name="test scope", actor="test")["scope_id"]


@pytest.fixture(autouse=True)
def banned_patterns(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the gate at a pattern nobody's real identity matches.

    The repository's own list is the one the commit hooks read, and a test that
    depends on it would either publish what it contains or start failing the
    day somebody edits it. One invented marker is enough to prove the refusal
    path, and pointing the environment variable here also proves the gate is
    never silently unchecked during a run.
    """
    path = tmp_path / "banned-patterns"
    path.write_text("SECRETMARKER\\d+\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(path))
