
from __future__ import annotations

import pytest

from mashu import capacity, memories, scopes, temporary
from mashu.errors import RefusedError
from mashu.tokens import pushed_cost

WINDOW = "the shared queue is down for maintenance until Friday"
RULE = "name the timezone in every scheduled job"


def test_a_window_longer_than_the_ceiling_is_a_permanent_claim_in_disguise(cur):
    with pytest.raises(RefusedError, match="pain_report"):
        temporary.put_temporary(cur, content=WINDOW, actor="agent", days=30)
    with pytest.raises(RefusedError):
        temporary.put_temporary(cur, content=WINDOW, actor="agent", days=0)

    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 0


def test_a_condition_comes_back_until_it_stops_applying(cur):
    row = temporary.put_temporary(cur, content=WINDOW, actor="agent", days=2.5)
    assert [r["context_id"] for r in temporary.active_temporary(cur)] == [row["context_id"]]

    cur.execute("UPDATE temporary_context SET expires_at = now() - INTERVAL '1 hour'")
    assert temporary.active_temporary(cur) == []


def test_a_scoped_session_still_sees_the_conditions_that_apply_everywhere(cur, scope_id):
    here = temporary.put_temporary(cur, content=WINDOW, actor="agent", days=3, scope_id=scope_id)
    anywhere = temporary.put_temporary(
        cur, content="the licence server is offline this week", actor="agent", days=3
    )
    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]

    at_home = {r["context_id"] for r in temporary.active_temporary(cur, scope_id=scope_id)}
    assert at_home == {here["context_id"], anywhere["context_id"]}

    away = [r["context_id"] for r in temporary.active_temporary(cur, scope_id=elsewhere)]
    assert away == [anywhere["context_id"]]

    unrouted = [r["context_id"] for r in temporary.active_temporary(cur)]
    assert unrouted == [anywhere["context_id"]]


def test_a_condition_takes_room_in_its_own_share_while_it_lasts(cur, monkeypatch):
    monkeypatch.setenv("MASHU_TEMPORARY_CAPACITY", "30")
    first = temporary.put_temporary(cur, content=WINDOW, actor="user", days=3)
    assert temporary.pushed_totals(cur)["always"] == pushed_cost([WINDOW])

    with pytest.raises(RefusedError, match="the temporary share seats 30 tokens") as refused:
        temporary.put_temporary(
            cur, content="the licence server is offline this week", actor="user", days=3
        )
    assert "lapses" in str(refused.value)

    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 1
    assert first["context_id"] is not None


def test_a_condition_does_not_spend_the_memory_seats(cur, monkeypatch):
    monkeypatch.setenv("MASHU_CAPACITY", "40")
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "40")
    temporary.put_temporary(cur, content=WINDOW, actor="user", days=3)

    assert capacity.bootstrap_totals(cur)["always"] == 0
    memories.remember(cur, content=RULE, actor="user")
    assert capacity.bootstrap_totals(cur)["always"] == pushed_cost([RULE])


def test_a_scoped_condition_weighs_on_that_scope(cur, scope_id):
    temporary.put_temporary(cur, content=WINDOW, actor="user", days=3, scope_id=scope_id)
    totals = temporary.pushed_totals(cur)
    assert totals["always"] == 0
    assert totals["scopes"] == {scope_id: pushed_cost([WINDOW])}
    assert totals["worst"] == pushed_cost([WINDOW])


def test_a_condition_for_everywhere_is_weighed_against_the_heaviest_scope(
    cur, scope_id, monkeypatch
):
    monkeypatch.setenv("MASHU_TEMPORARY_CAPACITY", "30")
    temporary.put_temporary(cur, content=WINDOW, actor="user", days=3, scope_id=scope_id)

    with pytest.raises(RefusedError, match="the temporary share seats 30 tokens"):
        temporary.put_temporary(
            cur, content="the licence server is offline this week", actor="user", days=3
        )


def test_a_condition_too_big_for_the_empty_share_is_not_told_to_wait(cur, monkeypatch):
    monkeypatch.setenv("MASHU_TEMPORARY_CAPACITY", "5")
    with pytest.raises(RefusedError, match="say this one shorter") as refused:
        temporary.put_temporary(cur, content=WINDOW, actor="user", days=3)
    assert "lapses" not in str(refused.value)


def test_an_expired_condition_stops_taking_up_room(cur):
    temporary.put_temporary(cur, content=WINDOW, actor="user", days=3)
    cur.execute("UPDATE temporary_context SET expires_at = now() - INTERVAL '1 hour'")
    totals = temporary.pushed_totals(cur)
    assert totals["always"] == 0 and totals["count"] == 0


def test_a_banned_pattern_is_refused_here_too(cur):
    with pytest.raises(RefusedError):
        temporary.put_temporary(
            cur, content="use the SECRETMARKER7 path this week", actor="agent", days=1
        )
    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 0
