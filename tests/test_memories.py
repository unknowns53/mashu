from __future__ import annotations

from uuid import uuid4

import pytest

from conftest import remember, retire
from mashu import match, memories, scopes, temporary
from mashu.errors import MashuError, RefusedError, RetiredConflictError

RULE = "never report a run as finished without the output that proves it"
REVISED = "never report a run as finished without pasting the output that proves it"
WITHDRAWN = "the tool now refuses on its own"
PINNED = "delegation goes to the reviewer, not to another writer"


def test_a_rule_recorded_by_hand_is_active_at_once_and_carries_its_own_evidence(cur):
    memory = remember(cur, RULE)
    assert memory["status"] == "active"
    assert memory["delivery"] == "always"
    assert len(memory["evidence"]) == 1

    cur.execute("SELECT * FROM ledger WHERE ledger_id = %s", (memory["evidence"][0],))
    assert cur.fetchone()["kind"] == "explicit"

    cur.execute("SELECT content FROM memory_revision WHERE memory_id = %s", (memory["memory_id"],))
    assert [row["content"] for row in cur.fetchall()] == [RULE]


def test_retiring_needs_a_reason_and_a_kind_new_retirements_may_use(cur):
    memory = remember(cur, RULE)
    with pytest.raises(MashuError, match="reason"):
        retire(cur, memory, "  ")
    with pytest.raises(MashuError, match="reserved for existing data and migration"):
        memories.retire(
            cur, memory["memory_id"], reason=WITHDRAWN, actor="user", retirement_kind="legacy"
        )
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"

    retired = retire(cur, memory, WITHDRAWN)
    assert retired["status"] == "retired"
    assert retired["retire_reason"] == WITHDRAWN
    assert retired["retired_at"] is not None


def test_a_reason_carrying_a_banned_pattern_does_not_retire_anything(cur):
    memory = remember(cur, RULE)
    with pytest.raises(RefusedError):
        retire(cur, memory, "superseded by SECRETMARKER9")

    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"
    assert match.similar_tombstones(cur, RULE) == []


def test_the_gate_says_when_it_did_not_run(cur, monkeypatch, tmp_path):
    memory = remember(cur, RULE)
    assert memory["unchecked"] is False
    assert "malformed" not in memory

    broken = tmp_path / "broken-patterns"
    broken.write_text("SECRETMARKER\\d+\n(unclosed\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(broken))
    assert retire(cur, memory, WITHDRAWN)["malformed"] == 1

    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "absent"))
    # REVISED matches the retired text, so conflict handling runs first.
    tombstone = match.similar_tombstones(cur, REVISED)[0]
    rewritten = remember(cur, REVISED, acknowledged_conflicts=[tombstone["memory_id"]])
    assert rewritten["unchecked"] is True


def test_a_retired_rule_answers_with_why_it_was_withdrawn_and_never_with_itself(cur):
    memory = remember(cur, RULE)
    retire(cur, memory, WITHDRAWN)

    found = match.similar_tombstones(cur, RULE)
    assert [row["memory_id"] for row in found] == [memory["memory_id"]]
    assert found[0]["retire_reason"] == WITHDRAWN
    assert {
        "memory_id",
        "retire_reason",
        "retired_at",
        "retirement_kind",
        "superseded_by",
        "relocated_to_kind",
        "relocated_to_id",
        "score",
    } == set(found[0])


def test_a_revision_keeps_what_the_rule_used_to_say(cur):
    memory = remember(cur, RULE)
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
    memory = remember(cur, RULE)
    retire(cur, memory, "superseded", "legacy")
    assert memories.get_memory(cur, memory["memory_id"])["retirement_kind"] == "legacy"

    with pytest.raises(MashuError, match="retired"):
        memories.revise(cur, memory["memory_id"], content=REVISED, actor="user")
    with pytest.raises(MashuError, match="retired"):
        retire(cur, memory, "again")


def test_superseded_links_must_exist_and_cannot_form_cycles(cur):
    first = remember(cur, "the loader checks the schema fingerprint")
    second = remember(cur, "the decoder checks the payload checksum")
    retire(
        cur, first, "the decoder rule replaces it", "superseded", superseded_by=second["memory_id"]
    )

    with pytest.raises(MashuError, match="cycle"):
        retire(
            cur,
            second,
            "the loader rule replaces it",
            "superseded",
            superseded_by=first["memory_id"],
        )
    assert memories.get_memory(cur, second["memory_id"])["status"] == "active"

    with pytest.raises(MashuError, match="no successor memory"):
        retire(cur, second, "the next rule replaces it", "superseded", superseded_by=uuid4())


def test_a_move_names_what_its_delivery_needs_and_leaving_guard_clears_the_act(cur, scope_id):
    memory = remember(cur, RULE)
    with pytest.raises(MashuError, match="action"):
        memories.set_delivery(cur, memory["memory_id"], delivery="guard", actor="user")
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

    pinned = memories.set_delivery(
        cur, memory["memory_id"], delivery="guard", actor="user", guard_action="Task"
    )
    assert (pinned["delivery"], pinned["guard_action"]) == ("guard", "Task")
    with pytest.raises(MashuError, match="scope"):
        memories.set_delivery(cur, memory["memory_id"], delivery="scope", actor="user")

    moved = memories.set_delivery(
        cur, memory["memory_id"], delivery="scope", actor="user", scope_id=scope_id
    )
    assert moved["delivery"] == "scope"
    assert moved["scope_id"] == scope_id
    assert moved["guard_action"] is None


def test_a_guard_with_no_scope_fires_everywhere_and_a_scoped_one_only_at_home(cur, scope_id):
    everywhere = remember(cur, RULE, delivery="guard", guard_action="Task")
    here = remember(cur, PINNED, scope_id=scope_id, delivery="guard", guard_action="Task")
    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]

    unscoped = memories.guard_pins(cur, action="Task")
    assert [row["memory_id"] for row in unscoped] == [everywhere["memory_id"]]

    at_home = memories.guard_pins(cur, action="Task", scope_id=scope_id)
    assert {row["memory_id"] for row in at_home} == {everywhere["memory_id"], here["memory_id"]}

    away = memories.guard_pins(cur, action="Task", scope_id=elsewhere)
    assert [row["memory_id"] for row in away] == [everywhere["memory_id"]]
    assert memories.guard_pins(cur, action="Bash", scope_id=scope_id) == []


def test_listing_a_scope_shows_what_lives_there_by_whatever_route(cur, scope_id):
    pushed = remember(cur, RULE, scope_id=scope_id, delivery="scope")
    pinned = remember(cur, PINNED, scope_id=scope_id, delivery="guard", guard_action="Task")
    remember(cur, "answer in the language that was asked")

    listed = {row["memory_id"] for row in memories.active_memories(cur, scope_id=scope_id)}
    assert listed == {pushed["memory_id"], pinned["memory_id"]}
    assert [row["memory_id"] for row in memories.scope_push_memories(cur, scope_id)] == [
        pushed["memory_id"]
    ]


def test_writing_a_retired_rule_back_stops_to_show_why_it_was_withdrawn(cur):
    kept = remember(cur, RULE)
    retire(cur, kept, WITHDRAWN)

    with pytest.raises(RetiredConflictError) as raised:
        remember(cur, REVISED)
    assert [row["memory_id"] for row in raised.value.tombstones] == [kept["memory_id"]]
    assert raised.value.tombstones[0]["retire_reason"] == WITHDRAWN
    # The withdrawn body is not handed back with its own tombstone.
    assert "content" not in raised.value.tombstones[0]

    cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
    assert cur.fetchone()["n"] == 0

    written = remember(cur, REVISED, acknowledged_conflicts=[kept["memory_id"]])
    assert written["status"] == "active"
    assert [row["memory_id"] for row in written["overrides"]] == [kept["memory_id"]]

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'memory_created' AND memory_id = %s",
        (written["memory_id"],),
    )
    assert cur.fetchone()["detail"]["overrides"] == [str(kept["memory_id"])]

    unrelated = remember(cur, "quotas on the shared queue reset at midnight every day")
    assert unrelated["overrides"] == []


def test_a_scoped_rule_moved_to_the_act_gate_can_be_freed_of_the_scope_it_came_from(cur, scope_id):
    memory = remember(cur, RULE, scope_id=scope_id, delivery="scope")

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


def test_an_always_memory_can_become_temporary_and_back(cur):
    original = remember(cur, RULE)

    converted = memories.convert_to_temporary(cur, original["memory_id"], days=2, actor="user")
    context = converted["temporary"]
    assert converted["memory"]["status"] == "retired"
    assert "converted to temporary context until" in converted["memory"]["retire_reason"]
    assert context["scope_id"] is None
    assert [row["context_id"] for row in temporary.active_temporary(cur)] == [context["context_id"]]

    restored = temporary.convert_to_memory(cur, context["context_id"], actor="user")
    assert restored["memory"]["content"] == RULE
    assert restored["memory"]["delivery"] == "always"
    assert restored["memory"]["status"] == "active"
    assert temporary.active_temporary(cur) == []

    cur.execute(
        "SELECT event_type FROM event_log WHERE event_type LIKE '%%converted%%' ORDER BY event_id"
    )
    assert [row["event_type"] for row in cur.fetchall()] == [
        "memory_converted_to_temporary",
        "temporary_converted_to_memory",
    ]


def test_a_scoped_memory_keeps_its_scope_through_temporary_conversion(cur, scope_id):
    original = remember(cur, RULE, scope_id=scope_id, delivery="scope")

    converted = memories.convert_to_temporary(cur, original["memory_id"], days=1, actor="user")
    context = converted["temporary"]
    assert context["scope_id"] == scope_id

    restored = temporary.convert_to_memory(cur, context["context_id"], actor="user")["memory"]
    assert restored["scope_id"] == scope_id
    assert restored["delivery"] == "scope"


def test_temporary_round_trip_does_not_override_an_unrelated_invalidated_memory(cur):
    source = remember(cur, RULE)
    retire(cur, remember(cur, REVISED), "the required output now comes from the build service")
    converted = memories.convert_to_temporary(cur, source["memory_id"], days=2, actor="user")
    context = converted["temporary"]

    with pytest.raises(RetiredConflictError):
        temporary.convert_to_memory(cur, context["context_id"], actor="user")
    assert memories.get_memory(cur, source["memory_id"])["retirement_kind"] == "relocated"
    assert [row["context_id"] for row in temporary.active_temporary(cur)] == [context["context_id"]]


def test_a_guard_memory_cannot_lose_its_action_by_becoming_temporary(cur):
    memory = remember(cur, RULE, delivery="guard", guard_action="Bash")

    with pytest.raises(MashuError, match="action would be lost"):
        memories.convert_to_temporary(cur, memory["memory_id"], days=1, actor="user")
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"


def test_a_refused_conversion_keeps_its_source_active(cur, monkeypatch):
    memory = remember(cur, RULE)
    monkeypatch.setenv("MASHU_TEMPORARY_CAPACITY", "1")

    with pytest.raises(RefusedError, match="temporary share"):
        memories.convert_to_temporary(cur, memory["memory_id"], days=1, actor="user")
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"
    assert temporary.active_temporary(cur) == []

    monkeypatch.delenv("MASHU_TEMPORARY_CAPACITY")
    context = temporary.put_temporary(cur, content="brief outage", actor="user", days=1)
    monkeypatch.setenv("MASHU_CAPACITY", "1")
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "1")

    with pytest.raises(RefusedError, match="seats 1 tokens"):
        temporary.convert_to_memory(cur, context["context_id"], actor="user")
    assert [row["context_id"] for row in temporary.active_temporary(cur)] == [context["context_id"]]
