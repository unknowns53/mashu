
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
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')

    dsn = f"dbname={TEST_DB}"
    applied = migrate(dsn)
    assert applied, "no migrations were applied to the test database"
    return dsn


@pytest.fixture(scope="session")
def committing_dsn() -> str:
    """A second database for tests that have to commit."""
    name = f"{TEST_DB}_committing"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = f"dbname={name}"
    migrate(dsn)
    return dsn


@pytest.fixture
def cur(test_dsn: str) -> Iterator[psycopg.Cursor]:
    """A cursor whose transaction is rolled back when the test ends."""
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
    """Point the gate at a pattern nobody's real identity matches."""
    path = tmp_path / "banned-patterns"
    path.write_text("SECRETMARKER\\d+\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(path))
