
from __future__ import annotations

import pytest

from mashu import match, memories, scopes
from mashu.errors import MashuError, RefusedError, RetiredConflictError

RULE = "never report a run as finished without the output that proves it"
REVISED = "never report a run as finished without pasting the output that proves it"
WITHDRAWN = "the tool now refuses on its own"


def test_a_rule_recorded_by_hand_is_active_at_once_and_carries_its_own_evidence(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    assert memory["status"] == "active"
    assert memory["delivery"] == "always"
    assert len(memory["evidence"]) == 1

    cur.execute("SELECT * FROM ledger WHERE ledger_id = %s", (memory["evidence"][0],))
    assert cur.fetchone()["kind"] == "explicit"

    cur.execute("SELECT content FROM memory_revision WHERE memory_id = %s", (memory["memory_id"],))
    assert [row["content"] for row in cur.fetchall()] == [RULE]


def test_retiring_needs_a_reason_because_the_reason_is_all_that_survives(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    with pytest.raises(MashuError, match="reason"):
        memories.retire(cur, memory["memory_id"], reason="  ", actor="user")

    retired = memories.retire(cur, memory["memory_id"], reason=WITHDRAWN, actor="user")
    assert retired["status"] == "retired"
    assert retired["retire_reason"] == WITHDRAWN
    assert retired["retired_at"] is not None


def test_a_reason_carrying_a_banned_pattern_does_not_retire_anything(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    with pytest.raises(RefusedError):
        memories.retire(
            cur, memory["memory_id"], reason="superseded by SECRETMARKER9", actor="user"
        )

    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"
    assert match.similar_tombstones(cur, RULE) == []


def test_the_gate_says_when_it_did_not_run(cur, monkeypatch, tmp_path):
    memory = memories.remember(cur, content=RULE, actor="user")
    assert memory["unchecked"] is False
    assert "malformed" not in memory

    broken = tmp_path / "broken-patterns"
    broken.write_text("SECRETMARKER\\d+\n(unclosed\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(broken))
    retired = memories.retire(cur, memory["memory_id"], reason=WITHDRAWN, actor="user")
    assert retired["malformed"] == 1

    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "absent"))
    # REVISED matches the retired text, so conflict handling runs first.
    rewritten = memories.remember(cur, content=REVISED, actor="user", override_retired=True)
    assert rewritten["unchecked"] is True


def test_a_retired_rule_answers_with_why_it_was_withdrawn_and_never_with_itself(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    memories.retire(cur, memory["memory_id"], reason=WITHDRAWN, actor="user")

    found = match.similar_tombstones(cur, RULE)
    assert [row["memory_id"] for row in found] == [memory["memory_id"]]
    assert found[0]["retire_reason"] == WITHDRAWN
    assert set(found[0]) == {"memory_id", "retire_reason", "retired_at", "score"}


def test_a_revision_keeps_what_the_rule_used_to_say(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    revised = memories.revise(
        cur, memory["memory_id"], content=REVISED, actor="user", note="say what to paste"
    )
    assert revised["content"] == REVISED

    cur.execute(
        "SELECT content, note FROM memory_revision WHERE memory_id = %s ORDER BY created_at",
        (memory["memory_id"],),
    )
    rows = cur.fetchall()
    assert [row["content"] for row in rows] == [RULE, REVISED]
    assert rows[1]["note"] == "say what to paste"


def test_a_withdrawn_rule_is_not_edited_back_into_life(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    memories.retire(cur, memory["memory_id"], reason="superseded", actor="user")

    with pytest.raises(MashuError, match="retired"):
        memories.revise(cur, memory["memory_id"], content=REVISED, actor="user")
    with pytest.raises(MashuError, match="retired"):
        memories.retire(cur, memory["memory_id"], reason="again", actor="user")


def test_moving_a_rule_to_the_act_gate_needs_the_act(cur):
    memory = memories.remember(cur, content=RULE, actor="user")
    with pytest.raises(MashuError, match="action"):
        memories.set_delivery(cur, memory["memory_id"], delivery="guard", actor="user")

    pinned = memories.set_delivery(
        cur, memory["memory_id"], delivery="guard", actor="user", guard_action="Task"
    )
    assert pinned["delivery"] == "guard"
    assert pinned["guard_action"] == "Task"


def test_moving_a_rule_into_a_scope_needs_a_scope_and_leaving_guard_clears_the_act(cur, scope_id):
    memory = memories.remember(cur, content=RULE, actor="user")
    memories.set_delivery(
        cur, memory["memory_id"], delivery="guard", actor="user", guard_action="Task"
    )
    with pytest.raises(MashuError, match="scope"):
        memories.set_delivery(cur, memory["memory_id"], delivery="scope", actor="user")

    moved = memories.set_delivery(
        cur, memory["memory_id"], delivery="scope", actor="user", scope_id=scope_id
    )
    assert moved["delivery"] == "scope"
    assert moved["scope_id"] == scope_id
    assert moved["guard_action"] is None


def test_a_guard_with_no_scope_fires_everywhere_and_a_scoped_one_only_at_home(cur, scope_id):
    everywhere = memories.remember(
        cur, content=RULE, actor="user", delivery="guard", guard_action="Task"
    )
    here = memories.remember(
        cur,
        content="delegation goes to the reviewer, not to another writer",
        actor="user",
        scope_id=scope_id,
        delivery="guard",
        guard_action="Task",
    )
    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]

    unscoped = memories.guard_pins(cur, action="Task")
    assert [row["memory_id"] for row in unscoped] == [everywhere["memory_id"]]

    at_home = memories.guard_pins(cur, action="Task", scope_id=scope_id)
    assert {row["memory_id"] for row in at_home} == {
        everywhere["memory_id"],
        here["memory_id"],
    }

    away = memories.guard_pins(cur, action="Task", scope_id=elsewhere)
    assert [row["memory_id"] for row in away] == [everywhere["memory_id"]]


def test_a_pin_for_another_act_does_not_answer_this_one(cur):
    memories.remember(cur, content=RULE, actor="user", delivery="guard", guard_action="Bash")
    assert memories.guard_pins(cur, action="Task") == []


def test_listing_a_scope_shows_what_lives_there_by_whatever_route(cur, scope_id):
    pushed = memories.remember(cur, content=RULE, actor="user", scope_id=scope_id, delivery="scope")
    pinned = memories.remember(
        cur,
        content="delegation goes to the reviewer, not to another writer",
        actor="user",
        scope_id=scope_id,
        delivery="guard",
        guard_action="Task",
    )
    memories.remember(cur, content="answer in the language that was asked", actor="user")

    listed = {row["memory_id"] for row in memories.active_memories(cur, scope_id=scope_id)}
    assert listed == {pushed["memory_id"], pinned["memory_id"]}
    assert [row["memory_id"] for row in memories.scope_push_memories(cur, scope_id)] == [
        pushed["memory_id"]
    ]


def test_writing_a_retired_rule_back_stops_to_show_why_it_was_withdrawn(cur):
    kept = memories.remember(cur, content=RULE, actor="user")
    memories.retire(cur, kept["memory_id"], reason=WITHDRAWN, actor="user")

    with pytest.raises(RetiredConflictError) as raised:
        memories.remember(cur, content=REVISED, actor="user")
    assert [row["memory_id"] for row in raised.value.tombstones] == [kept["memory_id"]]
    assert raised.value.tombstones[0]["retire_reason"] == WITHDRAWN
    # The withdrawn body is not handed back with its own tombstone.
    assert "content" not in raised.value.tombstones[0]

    cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
    assert cur.fetchone()["n"] == 0

    written = memories.remember(cur, content=REVISED, actor="user", override_retired=True)
    assert written["status"] == "active"
    assert [row["memory_id"] for row in written["overrides"]] == [kept["memory_id"]]

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'memory_created' AND memory_id = %s",
        (written["memory_id"],),
    )
    assert cur.fetchone()["detail"]["overrides"] == [str(kept["memory_id"])]


def test_an_unrelated_rule_is_not_stopped_by_somebody_elses_retirement(cur):
    kept = memories.remember(cur, content=RULE, actor="user")
    memories.retire(cur, kept["memory_id"], reason=WITHDRAWN, actor="user")

    written = memories.remember(
        cur, content="quotas on the shared queue reset at midnight every day", actor="user"
    )
    assert written["status"] == "active"
    assert written["overrides"] == []


def test_a_scoped_rule_moved_to_the_act_gate_can_be_freed_of_the_scope_it_came_from(cur, scope_id):
    memory = memories.remember(cur, content=RULE, actor="user", scope_id=scope_id, delivery="scope")

    kept = memories.set_delivery(
        cur, memory["memory_id"], delivery="guard", actor="user", guard_action="Bash"
    )
    assert kept["scope_id"] == scope_id
    assert memories.guard_pins(cur, action="Bash") == []

    freed = memories.set_delivery(
        cur,
        memory["memory_id"],
        delivery="guard",
        actor="user",
        guard_action="Bash",
        clear_scope=True,
    )
    assert freed["scope_id"] is None
    assert [row["memory_id"] for row in memories.guard_pins(cur, action="Bash")] == [
        memory["memory_id"]
    ]


def test_a_move_cannot_both_name_a_scope_and_clear_it(cur, scope_id):
    memory = memories.remember(cur, content=RULE, actor="user")
    with pytest.raises(MashuError, match="clear"):
        memories.set_delivery(
            cur,
            memory["memory_id"],
            delivery="guard",
            actor="user",
            guard_action="Bash",
            scope_id=scope_id,
            clear_scope=True,
        )
