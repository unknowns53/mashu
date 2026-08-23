"""Apply the numbered SQL files in migrations/ in order.

Plain SQL rather than a migration framework, because the schema is the
specification's own artefact and reading it should not require translating an
object model back into DDL.
"""

from __future__ import annotations

import pathlib

import psycopg

from mashu.db import connect

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"

_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migration (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migration_files(directory: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every migration, ordered by filename, which carries the sequence."""
    return sorted((directory or MIGRATIONS_DIR).glob("*.sql"))


def applied(conn: psycopg.Connection) -> set[str]:
    """Filenames already recorded as applied."""
    with conn.cursor() as cur:
        cur.execute(_LEDGER)
        cur.execute("SELECT filename FROM schema_migration")
        return {row["filename"] for row in cur.fetchall()}


def migrate(conninfo: str | None = None, directory: pathlib.Path | None = None) -> list[str]:
    """Apply every pending migration and return the filenames applied.

    Each file runs in its own transaction together with its ledger row, so a
    file that fails leaves no trace of itself.
    """
    done: list[str] = []
    with connect(conninfo) as conn:
        already = applied(conn)
        for path in migration_files(directory):
            if path.name in already:
                continue
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(path.read_text())
                cur.execute(
                    "INSERT INTO schema_migration (filename) VALUES (%s)",
                    (path.name,),
                )
            done.append(path.name)
    return done


if __name__ == "__main__":
    for name in migrate() or ["(nothing pending)"]:
        print(f"applied: {name}")
