"""Database connection and transaction helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "dbname=mashu"
DSN_ENV_VAR = "MASHU_DATABASE_URL"


def dsn() -> str:
    """The connection string, from the environment or the local default."""
    return os.environ.get(DSN_ENV_VAR) or DEFAULT_DSN


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Open a connection whose rows come back as dictionaries."""
    return psycopg.connect(conninfo or dsn(), row_factory=dict_row)


@contextmanager
def transaction(conninfo: str | None = None) -> Iterator[psycopg.Cursor]:
    """Yield a cursor inside one transaction, committing on a clean exit."""
    with connect(conninfo) as conn, conn.transaction(), conn.cursor() as cur:
        yield cur
