from __future__ import annotations

import pytest

from conftest import candidate, keys, propose_change, read_through, remember, retire
from mashu import db, memories, memory_change_ui, memory_changes, nominations, screen, topics
from mashu.temporary import put_temporary

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


@pytest.mark.parametrize("key,status", [("y", "applied"), ("d", "declined"), ("w", "withdrawn")])
def test_color_does_not_leave_decided_proposals_in_the_queue(dsn, monkeypatch, capsys, key, status):
    proposal = _proposal(dsn)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert screen.success("  ✓ done").startswith("\x1b[")
    keys(monkeypatch, "", key, *(("because",) if key != "y" else ()))
    assert memory_change_ui.run(dsn) == 0
    assert capsys.readouterr().out.count("target Memory") == 1
    with db.transaction(dsn) as cur:
        assert memory_changes.get(cur, proposal["change_id"])["status"] == status
    assert memory_change_ui._queue(dsn) == []


def test_replace_detail_shows_the_successor_snapshot_and_delivery(cur):
    home = topics.create_topic(cur, name="deploy", trigger="Before releasing", actor="user")
    old = remember(
        cur, "the old signature check blocks releases", delivery="topic", topic_id=home["topic_id"]
    )
    successor = candidate(
        cur, "verify the signed archive envelope before extraction", kind="user_explicit"
    )
    proposal = propose_change(
        cur,
        old,
        "replace",
        retirement_kind="superseded",
        retire_reason="the signed archive rule replaces the old check",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
    )
    detail = memory_change_ui._detail(proposal, 1, 1)
    assert "verify the signed archive envelope before extraction" in detail
    assert "replacement delivery: topic:deploy" in detail
    assert f"candidate version: {successor['version']}" in detail
    assert "candidate scope: -" in detail
    assert "candidate kind: user_explicit" in detail

    revised = "verify the archive envelope and checksum before extraction"
    nominations.revise(cur, successor["nomination_id"], content=revised, actor="user")
    stale = memory_change_ui._detail(memory_changes.get(cur, proposal["change_id"]), 1, 1)
    assert "successor nomination changed after this proposal was read" in stale
    assert revised in stale


def test_redeliver_detail_shows_where_the_rule_moves(cur, scope_id):
    memory = remember(cur, "keep ship stats fixed when tuning")
    proposal = propose_change(
        cur,
        memory,
        "redeliver",
        successor_settings={
            "delivery": "topic",
            "scope_id": scope_id,
            "topic": "calibration",
            "topic_trigger": "Before calibrating difficulty",
        },
    )

    shown = memory_change_ui._detail(proposal, 1, 1)
    assert "delivery: always -> topic:calibration" in shown


def test_relocated_proposal_shows_its_own_destination(cur, scope_id):
    target = remember(cur, RULE)
    context = put_temporary(cur, content="archive check", actor="user", days=2, scope_id=scope_id)
    proposal = propose_change(
        cur,
        target,
        "retire",
        retirement_kind="relocated",
        retire_reason=REASON,
        relocated_to_kind="temporary_context",
        relocated_to_id=context["context_id"],
    )
    shown = memory_change_ui._detail(proposal, 1, 1)
    assert str(context["context_id"]) in shown
    assert context["content"] in shown
    assert "destination scope: test scope" in shown
    assert str(context["expires_at"]) in shown


def test_stale_proposal_shows_new_short_target_from_the_first_page(dsn, monkeypatch, capsys):
    row = _proposal(dsn)
    row["target"]["content"] = "\n".join(f"old wording {n}" for n in range(30))
    monkeypatch.setenv("LINES", "12")
    monkeypatch.setattr(memory_change_ui, "_queue", lambda _dsn: [row])
    read_through(
        monkeypatch, memory_change_ui._detail(row, 1, 1), memory_change_ui._DETAIL_KEYS, "y", "q"
    )
    getkey = screen.getkey

    def change_before_apply():
        key = getkey()
        if key == "y":
            with db.transaction(dsn) as cur:
                memories.revise(
                    cur, row["target_memory_id"], content="new short target", actor="user"
                )
        return key

    monkeypatch.setattr(screen, "getkey", change_before_apply)
    memory_change_ui.run(dsn)
    assert "new short target" in capsys.readouterr().out


def test_restore_acknowledges_conflict_only_after_its_reason_is_shown(dsn, monkeypatch):
    body = "\n".join(f"check archive index step {n}" for n in range(30))
    with db.transaction(dsn) as cur:
        target = remember(cur, body)
        conflict = remember(cur, body)
        retire(cur, target, "scope ended", kind="out_of_scope")
        retire(cur, conflict, "unsafe now")
        proposal = propose_change(cur, target, "restore", restore_reason="needed again")
    monkeypatch.setenv("LINES", "12")
    keys(monkeypatch, "", "y", "q")
    memory_change_ui.run(dsn)
    with db.transaction(dsn) as cur:
        assert memory_changes.get(cur, proposal["change_id"])["status"] == "pending"

    read_through(
        monkeypatch, memory_change_ui._detail(proposal, 1, 1), memory_change_ui._DETAIL_KEYS, "y"
    )
    memory_change_ui.run(dsn)
    with db.transaction(dsn) as cur:
        assert memory_changes.get(cur, proposal["change_id"])["status"] == "applied"
        cur.execute(
            "SELECT detail FROM event_log WHERE event_type = 'memory_restored' AND memory_id = %s",
            (target["memory_id"],),
        )
        source = cur.fetchone()["detail"]["approval_source"]
    assert source["conflict_ids"] == [str(conflict["memory_id"])]
    assert source["conflict_acknowledged_by_action"] is True
