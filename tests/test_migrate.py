"""Where the schema stands, read without changing it.

Code and schema travel separately — a checkout moves with a pull, a database
only when somebody runs the migration — so the gap between them is a state the
store can be in, and one that no other reading reveals: every tool whose INSERT
names a new column fails on every call, and the failure surfaces only in the
caller's error.
"""

from __future__ import annotations

from mashu import migrate


def test_a_migration_on_disk_and_not_in_the_database_is_pending(cur, tmp_path):
    (tmp_path / "0001_init.sql").write_text("", encoding="utf-8")
    (tmp_path / "9998_first_unapplied.sql").write_text("", encoding="utf-8")
    (tmp_path / "9999_second_unapplied.sql").write_text("", encoding="utf-8")

    assert migrate.pending(cur, tmp_path) == [
        "9998_first_unapplied.sql",
        "9999_second_unapplied.sql",
    ]


def test_a_fully_applied_schema_is_pending_nothing(cur):
    assert migrate.pending(cur) == []


def test_reading_the_schema_does_not_create_the_ledger_it_reads(cur, tmp_path):
    """The reader runs inside `status`, which must not write to answer a question.

    A database that has never been migrated answers 'nothing applied' rather
    than gaining an empty ledger, so the first `status` on a fresh store still
    says what is missing.
    """
    cur.execute("DROP TABLE schema_migration")
    (tmp_path / "0001_init.sql").write_text("", encoding="utf-8")

    assert migrate.pending(cur, tmp_path) == ["0001_init.sql"]
    cur.execute("SELECT to_regclass('schema_migration') AS reg")
    assert cur.fetchone()["reg"] is None
