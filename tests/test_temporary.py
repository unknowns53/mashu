"""Conditions that expire on their own (specification 7)."""

from __future__ import annotations

import pytest

from mashu import capacity, scopes, temporary
from mashu.errors import RefusedError
from mashu.tokens import pushed_cost

WINDOW = "the shared queue is down for maintenance until Friday"


def test_a_window_longer_than_the_ceiling_is_a_permanent_claim_in_disguise(cur):
    """Fourteen days is the whole of the discipline, so the refusal points elsewhere."""
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


def test_a_condition_takes_a_seat_while_it_lasts(cur, monkeypatch):
    """Expiring on its own is why it needs no review, not why it is weightless.

    It is pushed in the same opening as the memories, so leaving it out of the
    count made the ceiling report a smaller opening than the one being sent.
    """
    monkeypatch.setenv("MASHU_CAPACITY", "30")
    first = temporary.put_temporary(cur, content=WINDOW, actor="user", days=3)
    assert capacity.bootstrap_totals(cur)["always"] == pushed_cost([WINDOW])

    with pytest.raises(RefusedError, match="seats 30 tokens"):
        temporary.put_temporary(
            cur, content="the licence server is offline this week", actor="user", days=3
        )

    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 1
    assert first["context_id"] is not None


def test_a_scoped_condition_weighs_on_that_scope(cur, scope_id, monkeypatch):
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    temporary.put_temporary(cur, content=WINDOW, actor="user", days=3, scope_id=scope_id)
    totals = capacity.bootstrap_totals(cur)
    assert totals["always"] == 0
    assert totals["scopes"] == {scope_id: pushed_cost([WINDOW])}


def test_an_expired_condition_stops_taking_up_room(cur, monkeypatch):
    monkeypatch.setenv("MASHU_CAPACITY", "400")
    temporary.put_temporary(cur, content=WINDOW, actor="user", days=3)
    cur.execute("UPDATE temporary_context SET expires_at = now() - INTERVAL '1 hour'")
    assert capacity.bootstrap_totals(cur)["always"] == 0


def test_a_banned_pattern_is_refused_here_too(cur):
    """The entrance check does not care which table the text was heading for."""
    with pytest.raises(RefusedError):
        temporary.put_temporary(
            cur, content="use the SECRETMARKER7 path this week", actor="agent", days=1
        )
    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 0
