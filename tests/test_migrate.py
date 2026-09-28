from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from mashu import migrate

LEGACY_REASON = "converted to temporary context until 2020"
ACTIVE = "SELECT memory_id FROM memory WHERE status = 'active' ORDER BY memory_id"


def test_a_migration_on_disk_and_not_in_the_database_is_pending(cur, tmp_path):
    assert migrate.pending(cur) == []
    (tmp_path / "0001_init.sql").write_text("", encoding="utf-8")
    (tmp_path / "9998_first_unapplied.sql").write_text("", encoding="utf-8")
    (tmp_path / "9999_second_unapplied.sql").write_text("", encoding="utf-8")

    assert migrate.pending(cur, tmp_path) == [
        "9998_first_unapplied.sql",
        "9999_second_unapplied.sql",
    ]


def test_reading_the_schema_does_not_create_the_ledger_it_reads(cur, tmp_path):
    cur.execute("DROP TABLE schema_migration")
    (tmp_path / "0001_init.sql").write_text("", encoding="utf-8")

    assert migrate.pending(cur, tmp_path) == ["0001_init.sql"]
    cur.execute("SELECT to_regclass('schema_migration') AS reg")
    assert cur.fetchone()["reg"] is None


def test_retirement_upgrade_preserves_active_set_and_uses_only_structured_conversion_events(
    old_store,
):
    dsn = old_store("0008_memory_change_proposals.sql")
    with psycopg.connect(dsn) as conn:
        ledger_id = conn.execute(
            "INSERT INTO ledger (kind, what, prevention, created_by) "
            "VALUES ('explicit', 'migration seed', 'migration seed rule', 'test') "
            "RETURNING ledger_id"
        ).fetchone()[0]
        context_id = conn.execute(
            "INSERT INTO temporary_context (content, created_by, expires_at) "
            "VALUES ('the moved condition', 'test', now() + interval '2 days') "
            "RETURNING context_id"
        ).fetchone()[0]

        def memory(content, status="active", reason=None):
            memory_id = conn.execute(
                "INSERT INTO memory (content, delivery, status, evidence, retire_reason, "
                "retired_at, created_by) VALUES (%s, 'always', %s, %s, %s, "
                "CASE WHEN %s::text = 'retired' THEN now() END, 'test') RETURNING memory_id",
                (content, status, [ledger_id], reason, status),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO memory_revision (memory_id, content, actor) VALUES (%s, %s, 'test')",
                (memory_id, content),
            )
            return memory_id

        active_id = memory("active rule survives migration")
        legacy_id = memory("old conversion wording", "retired", LEGACY_REASON)
        relocated_id = memory(
            "structured conversion", "retired", "converted to temporary context until tomorrow"
        )
        conn.execute(
            "INSERT INTO event_log (event_type, actor, memory_id, detail) "
            "VALUES ('memory_converted_to_temporary', 'user', %s, %s)",
            (relocated_id, Jsonb({"context_id": str(context_id)})),
        )
        active_before = conn.execute(ACTIVE).fetchall()

    assert "0008_memory_change_proposals.sql" in migrate.migrate(dsn)
    with psycopg.connect(dsn) as conn:
        assert conn.execute(ACTIVE).fetchall() == active_before == [(active_id,)]
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT memory_id, retirement_kind, relocated_to_kind, "
                "relocated_to_id, retire_reason FROM memory "
                "WHERE status = 'retired'"
            )
        }
        assert rows[legacy_id] == ("legacy", None, None, LEGACY_REASON)
        assert rows[relocated_id][:3] == ("relocated", "temporary_context", context_id)
        classified = "SELECT count(*) FROM event_log WHERE event_type = %s"
        assert conn.execute(classified, ("memory_retirement_classified",)).fetchone()[0] == 2
