"""Connection handling.

One rule holds throughout the layer: a knowledge state change and the event_log
rows that describe it belong to the same transaction (specification 26). So the
store takes a cursor rather than opening its own connection, and the caller
decides where the transaction boundary sits.
"""

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
    """Yield a cursor inside one transaction, committing on a clean exit.

    Any exception rolls the whole thing back, which is what keeps a partially
    switched pointer or an event row without its change from ever landing.
    """
    with connect(conninfo) as conn, conn.transaction(), conn.cursor() as cur:
        yield cur
