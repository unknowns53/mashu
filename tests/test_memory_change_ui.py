from __future__ import annotations

import io
import os

import psycopg
import pytest

from mashu import db, memories, memory_change_ui, memory_changes, nominations
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_memory_change_ui"
RULE = "the signed archive index is checked before extraction"


@pytest.fixture
def dsn() -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    conninfo = f"dbname={TEST_DB}"
    migrate(conninfo)
    return conninfo


@pytest.fixture(autouse=True)
def a_screen_of_a_known_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "40")


def _proposal(dsn: str):
    with db.transaction(dsn) as cur:
        memory = memories.remember(cur, content=RULE, actor="user")
        detail = memories.memory_details(cur, memory["memory_id"])
        return memory_changes.propose(
            cur,
            target_memory_id=memory["memory_id"],
            target_revision_id=detail["current_revision_id"],
            target_updated_at=detail["updated_at"],
            operation="retire",
            retirement_kind="out_of_scope",
            retire_reason="the archived format is no longer used",
            evidence=[
                {
                    "kind": "ledger",
                    "id": str(memory["evidence"][0]),
                    "observation": "the archive path no longer uses this check",
                }
            ],
            actor="agent",
        )


def _keys(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{key}\n" for key in keys)))


def test_change_review_shows_the_full_target_and_applies_on_one_key(dsn, monkeypatch, capsys):
    proposal = _proposal(dsn)
    _keys(monkeypatch, "", "y")

    assert memory_change_ui.run(dsn) == 0
    output = capsys.readouterr().out
    assert RULE in output
    assert "out_of_scope" in output
    assert "the archived format is no longer used" in output
    with db.transaction(dsn) as cur:
        memory = memories.get_memory(cur, proposal["target_memory_id"])
        assert memory["status"] == "retired"
        assert memory["retirement_kind"] == "out_of_scope"
        cur.execute(
            "SELECT actor, detail FROM event_log WHERE event_type = 'memory_retired' "
            "AND memory_id = %s",
            (memory["memory_id"],),
        )
        event = cur.fetchone()
        assert event["actor"] == "user"
        assert event["detail"]["approval_source"]["kind"] == "user_direct"


def test_change_review_edits_the_prefilled_reason_before_apply(dsn, monkeypatch, capsys):
    proposal = _proposal(dsn)
    _keys(monkeypatch, "", "e", "the archived format moved to a separate worker", "y")

    assert memory_change_ui.run(dsn) == 0
    output = capsys.readouterr().out
    assert "the archived format is no longer used" in output
    assert "the archived format moved to a separate worker" in output
    with db.transaction(dsn) as cur:
        memory = memories.get_memory(cur, proposal["target_memory_id"])
        assert memory["retire_reason"] == "the archived format moved to a separate worker"
        change = memory_changes.get(cur, proposal["change_id"])
        assert change["version"] == 2
        assert change["status"] == "applied"


def test_replace_detail_shows_the_successor_snapshot_and_delivery(dsn):
    with db.transaction(dsn) as cur:
        old = memories.remember(
            cur,
            content="the old deployment signature guard blocks unsigned releases",
            actor="user",
            delivery="guard",
            guard_action="deploy",
        )
        cur.execute(
            "INSERT INTO ledger (kind, what, prevention, created_by) "
            "VALUES ('explicit', 'replacement evidence', "
            "'verify the signed archive envelope', 'user') "
            "RETURNING ledger_id"
        )
        evidence_id = cur.fetchone()["ledger_id"]
        successor = nominations.create_nomination(
            cur,
            content="verify the signed archive envelope before extraction",
            kind="user_explicit",
            evidence=[evidence_id],
            actor="agent",
        )
        target = memories.memory_details(cur, old["memory_id"])
        proposal = memory_changes.propose(
            cur,
            target_memory_id=old["memory_id"],
            target_revision_id=target["current_revision_id"],
            target_updated_at=target["updated_at"],
            operation="replace",
            retirement_kind="superseded",
            retire_reason="the signed archive rule replaces the old guard",
            successor_nomination_id=successor["nomination_id"],
            successor_nomination_version=successor["version"],
            evidence=[
                {
                    "kind": "ledger",
                    "id": str(evidence_id),
                    "observation": "the replacement checks the release envelope",
                }
            ],
            actor="agent",
        )
        detail = memory_change_ui._detail(proposal, 1, 1)
        assert "verify the signed archive envelope before extraction" in detail
        assert "replacement delivery: guard: deploy" in detail
        assert f"candidate version: {successor['version']}" in detail
        assert "candidate scope: -" in detail
        assert "candidate kind: user_explicit" in detail

        nominations.revise(
            cur,
            successor["nomination_id"],
            content="verify the archive envelope and checksum before extraction",
            actor="user",
        )
        stale = memory_changes.get(cur, proposal["change_id"])
        detail = memory_change_ui._detail(stale, 1, 1)
        assert "successor nomination changed after this proposal was read" in detail
        assert "verify the archive envelope and checksum before extraction" in detail
