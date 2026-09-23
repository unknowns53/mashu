from __future__ import annotations

import pytest

from conftest import candidate, keys, propose_change, remember
from mashu import db, memories, memory_change_ui, memory_changes, nominations

RULE = "the signed archive index is checked before extraction"
REASON = "the archived format is no longer used"

pytestmark = pytest.mark.usefixtures("known_screen")


def _proposal(dsn: str):
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
        return propose_change(
            cur, memory, "retire", retirement_kind="out_of_scope", retire_reason=REASON
        )


def test_change_review_shows_the_full_target_and_applies_on_one_key(dsn, monkeypatch, capsys):
    proposal = _proposal(dsn)
    keys(monkeypatch, "", "y")

    assert memory_change_ui.run(dsn) == 0
    output = capsys.readouterr().out
    assert RULE in output
    assert "out_of_scope" in output
    assert REASON in output
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


def test_change_review_edits_the_prefilled_reason_before_apply(dsn, monkeypatch):
    proposal = _proposal(dsn)
    keys(monkeypatch, "", "e", "the archived format moved to a separate worker", "y")

    assert memory_change_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        memory = memories.get_memory(cur, proposal["target_memory_id"])
        assert memory["retire_reason"] == "the archived format moved to a separate worker"
        change = memory_changes.get(cur, proposal["change_id"])
        assert change["version"] == 2
        assert change["status"] == "applied"


def test_replace_detail_shows_the_successor_snapshot_and_delivery(cur):
    old = remember(
        cur, "the old signature guard blocks releases", delivery="guard", guard_action="deploy"
    )
    successor = candidate(
        cur, "verify the signed archive envelope before extraction", kind="user_explicit"
    )
    proposal = propose_change(
        cur,
        old,
        "replace",
        retirement_kind="superseded",
        retire_reason="the signed archive rule replaces the old guard",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
    )
    detail = memory_change_ui._detail(proposal, 1, 1)
    assert "verify the signed archive envelope before extraction" in detail
    assert "replacement delivery: guard:deploy" in detail
    assert f"candidate version: {successor['version']}" in detail
    assert "candidate scope: -" in detail
    assert "candidate kind: user_explicit" in detail

    revised = "verify the archive envelope and checksum before extraction"
    nominations.revise(cur, successor["nomination_id"], content=revised, actor="user")
    stale = memory_change_ui._detail(memory_changes.get(cur, proposal["change_id"]), 1, 1)
    assert "successor nomination changed after this proposal was read" in stale
    assert revised in stale


def test_redeliver_detail_shows_where_the_rule_moves_and_the_topic_it_opens(cur, scope_id):
    memory = remember(cur, "keep ship stats fixed when tuning")
    proposal = propose_change(
        cur,
        memory,
        "redeliver",
        successor_settings={
            "delivery": "topic",
            "scope_id": scope_id,
            "guard_action": None,
            "topic": "calibration",
            "topic_trigger": "Before calibrating difficulty",
        },
    )

    shown = memory_change_ui._detail(proposal, 1, 1)
    assert "delivery: always -> topic:calibration" in shown
    assert "opens topic calibration for test scope: Before calibrating difficulty" in shown
    assert "redeliver" in memory_change_ui._summary(proposal)
