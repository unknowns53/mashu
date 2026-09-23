from __future__ import annotations

import psycopg
import pytest

from conftest import new_project, new_task, remember

TASK_ROW = "INSERT INTO {table} (task_id, {columns}) VALUES (%s, {values})"
# table, the columns a task-history row needs besides its task, their values, and a rewrite
HISTORY = [
    ("task_checkpoint", "what_changed, created_by", "'the tables landed', 'agent'", "what_changed"),
    ("attempt", "attempt, result, created_by", "'split it', 'it failed', 'agent'", "result"),
    (
        "decision",
        "decision, reason, created_by",
        "'keep one', 'half is in public', 'agent'",
        "reason",
    ),
    ("artifact_reference", "kind, locator", "'git_commit', '9d12f5a'", "locator"),
]


@pytest.fixture
def seeded(cur):
    """One row in every append-only table."""
    remember(cur, "answer in the language that was asked")
    new_project(cur, "mashu")
    task_id = new_task(cur, "implement the v3 schema", "mashu")["task"]["task_id"]
    for table, columns, values, _ in HISTORY:
        cur.execute(TASK_ROW.format(table=table, columns=columns, values=values), (task_id,))
    return cur


REWRITES = [
    ("ledger", "what"),
    ("memory_revision", "content"),
    ("event_log", "actor"),
    *[(table, column) for table, _, _, column in HISTORY],
]


@pytest.mark.parametrize(("table", "column"), REWRITES)
def test_the_history_refuses_to_be_rewritten_or_deleted(seeded, table, column):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with seeded.connection.transaction():
            seeded.execute(f"UPDATE {table} SET {column} = 'something else'")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        seeded.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("table", [row[0] for row in HISTORY])
def test_the_task_history_refuses_to_be_emptied(cur, table):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute(f"TRUNCATE {table}")
