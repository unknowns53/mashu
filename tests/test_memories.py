"""Memories: the only rows delivered as true (specification 5)."""

from __future__ import annotations

import pytest

from mashu import match, memories, scopes
from mashu.errors import MashuError

RULE = "never report a run as finished without the output that proves it"
REVISED = "never report a run as finished without pasting the output that proves it"
WITHDRAWN = "the tool now refuses on its own"


def test_a_rule_recorded_by_hand_is_active_at_once_and_carries_its_own_evidence(cur):
    """The schema will not hold a rule with nothing under it, so the act of
    recording is itself entered in the ledger and cited."""
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


def test_a_retired_rule_answers_with_why_it_was_withdrawn_and_never_with_itself(cur):
    """Handing the body back would put the withdrawn claim into circulation again."""
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
    """Reviving it would leave the tombstone standing next to a live claim."""
    memory = memories.remember(cur, content=RULE, actor="user")
    memories.retire(cur, memory["memory_id"], reason="superseded", actor="user")

    with pytest.raises(MashuError, match="retired"):
        memories.revise(cur, memory["memory_id"], content=REVISED, actor="user")
    with pytest.raises(MashuError, match="retired"):
        memories.retire(cur, memory["memory_id"], reason="again", actor="user")


def test_moving_a_rule_to_the_act_gate_needs_the_act(cur):
    """A guard with no action fires on nothing, which is a rule that was deleted quietly."""
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
    """Guard is orthogonal to scope: one narrows when, the other narrows where."""
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
    """A guard rule costs the opening nothing but still occupies a person's attention."""
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
