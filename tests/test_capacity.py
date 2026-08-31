"""The seat count (specification 5.2).

The numbers are kept small with MASHU_CAPACITY rather than by writing long
fixtures: what is being tested is which totals an addition is weighed against,
and that is the same arithmetic at any ceiling.
"""

from __future__ import annotations

import pytest

from mashu import capacity, config, memories, scopes, temporary
from mashu.errors import RefusedError
from mashu.tokens import pushed_cost

# The same length each, so every one of them costs the same and the arithmetic
# below can be read without running it.
RULES = [f"keep the first rule about ordering ok {i}" for i in range(5)]
NEW_RULE = "keep another rule about ordering here okay"
CONDITION = "the shared queue is down for maintenance until Friday"
CONDITION_EVERYWHERE = "the licence server is offline this week"


def fill(cur, scope_id, count):
    for content in RULES[:count]:
        memories.remember(cur, content=content, actor="user", scope_id=scope_id, delivery="scope")


def test_an_empty_store_admits_anything_reasonable(cur, scope_id):
    got = capacity.check_admission(cur, content=NEW_RULE, delivery="always")
    assert got["ok"] is True
    assert got["refusal"] is None
    assert got["tokens"] == pushed_cost([NEW_RULE])
    assert got["capacity"] == config.capacity()


def test_the_act_gate_is_never_full(cur, monkeypatch):
    """Guard rows are not in the opening, so there is nothing to weigh them against.

    This is what makes stepping down to guard a real way out of a refusal
    rather than an equivalent refusal one command later.
    """
    monkeypatch.setenv("MASHU_CAPACITY", "10")
    got = capacity.check_admission(cur, content=NEW_RULE * 20, delivery="guard")
    assert got["ok"] is True
    assert got["refusal"] is None


def test_a_full_scope_refuses_the_next_rule_and_says_by_how_much(cur, scope_id, monkeypatch):
    """A gate that only says no teaches nothing, and the reader is holding a rule."""
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    fill(cur, scope_id, 5)

    monkeypatch.setenv("MASHU_CAPACITY", "100")
    got = capacity.check_admission(cur, content=NEW_RULE, delivery="scope", scope_id=scope_id)
    assert got["ok"] is False
    assert str(got["capacity"]) in got["refusal"]
    assert str(got["projected"]) in got["refusal"]
    assert "mashu retire" in got["refusal"] and "guard" in got["refusal"]

    with pytest.raises(RefusedError, match="seats 100 tokens"):
        memories.remember(cur, content=NEW_RULE, actor="user", scope_id=scope_id, delivery="scope")


def test_an_always_rule_is_weighed_against_the_fullest_scope(cur, scope_id, monkeypatch):
    """It rides with every session in turn, including the session that opens heaviest.

    Weighing it against the always layer alone would admit rules that leave
    the loaded scope's opening over the ceiling, and nothing downstream looks
    at that again.
    """
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    light = scopes.create_scope(cur, name="a light scope", actor="user")["scope_id"]
    fill(cur, scope_id, 4)
    fill(cur, light, 1)

    got = capacity.check_admission(cur, content=NEW_RULE, delivery="always")
    heaviest = pushed_cost(RULES[:4])
    assert got["projected"] == heaviest + pushed_cost([NEW_RULE])

    # The same addition weighed against the light scope alone would have fitted.
    monkeypatch.setenv("MASHU_CAPACITY", str(got["projected"] - 1))
    assert capacity.check_admission(cur, content=NEW_RULE, delivery="always")["ok"] is False
    assert (
        capacity.check_admission(cur, content=NEW_RULE, delivery="scope", scope_id=light)["ok"]
        is True
    )


def test_a_rule_being_rewritten_is_not_competing_with_itself(cur, scope_id, monkeypatch):
    """Without the exclusion every edit to a full store is refused for its own space."""
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    fill(cur, scope_id, 5)
    subject = memories.scope_push_memories(cur, scope_id)[0]

    # Exactly enough room for four of the five plus the replacement.
    monkeypatch.setenv("MASHU_CAPACITY", str(pushed_cost(RULES[:4]) + pushed_cost([NEW_RULE])))
    fresh = capacity.check_admission(cur, content=NEW_RULE, delivery="scope", scope_id=scope_id)
    in_place = capacity.check_admission(
        cur,
        content=NEW_RULE,
        delivery="scope",
        scope_id=scope_id,
        exclude_memory_id=subject["memory_id"],
    )
    assert fresh["ok"] is False
    assert in_place["ok"] is True

    revised = memories.revise(cur, subject["memory_id"], content=NEW_RULE, actor="user")
    assert revised["content"] == NEW_RULE


def test_the_totals_separate_what_every_session_pays_from_what_one_scope_does(
    cur, scope_id, monkeypatch
):
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    fill(cur, scope_id, 2)
    memories.remember(cur, content=NEW_RULE, actor="user")

    totals = capacity.bootstrap_totals(cur)
    assert totals["always"] == pushed_cost([NEW_RULE])
    assert totals["scopes"] == {scope_id: pushed_cost(RULES[:2])}
    assert totals["worst"] == totals["always"] + pushed_cost(RULES[:2])


def test_the_always_layer_has_a_lower_ceiling_of_its_own(cur, scope_id, monkeypatch):
    """It can have room in the opening and still be refused.

    Without this the layer never overflows: it spends the headroom the scopes
    were going to need, and they find the room gone without having been
    refused anything.
    """
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(pushed_cost(RULES[:1])))
    memories.remember(cur, content=RULES[0], actor="user")

    got = capacity.check_admission(cur, content=NEW_RULE, delivery="always")
    assert got["ok"] is False
    assert got["capacity"] == config.always_capacity()
    assert got["projected"] == pushed_cost(RULES[:1]) + pushed_cost([NEW_RULE])
    # The whole opening had room for it; the layer is what refused.
    assert got["projected"] < config.capacity()

    # The same rule, told which one place it governs, fits.
    assert (
        capacity.check_admission(cur, content=NEW_RULE, delivery="scope", scope_id=scope_id)["ok"]
        is True
    )


def test_the_layer_refusal_names_the_door_a_scope_rule_does_not_have(cur, monkeypatch):
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "10")
    got = capacity.check_admission(cur, content=NEW_RULE, delivery="always")
    assert got["ok"] is False
    assert "the always layer seats 10 tokens" in got["refusal"]
    assert "mashu deliver <id> scope" in got["refusal"]

    with pytest.raises(RefusedError, match="the always layer seats"):
        memories.remember(cur, content=NEW_RULE, actor="user")


def test_a_dated_condition_is_not_weighed_against_the_seats(cur, scope_id, monkeypatch):
    """The seats count memories now, and nothing else (v3 8).

    v2 weighed the temporary contexts here on the ground that they are pushed
    in the same opening. The arithmetic was right and the room was wrong: a
    fortnight of outages could refuse a rule that had cost something to learn,
    which is one subsystem spending another's share.
    """
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    temporary.put_temporary(cur, content=CONDITION, actor="user", days=3, scope_id=scope_id)
    temporary.put_temporary(cur, content=CONDITION_EVERYWHERE, actor="user", days=3)

    totals = capacity.bootstrap_totals(cur)
    assert totals == {"always": 0, "scopes": {}, "worst": 0}

    monkeypatch.setenv("MASHU_CAPACITY", str(pushed_cost([NEW_RULE])))
    assert capacity.check_admission(cur, content=NEW_RULE, delivery="always")["ok"] is True


def test_a_scope_rule_is_not_weighed_against_the_layer_ceiling(cur, scope_id, monkeypatch):
    """The lower ceiling is about what every session pays, not about size."""
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "1")
    got = capacity.check_admission(cur, content=NEW_RULE, delivery="scope", scope_id=scope_id)
    assert got["ok"] is True
    assert got["capacity"] == config.capacity()
