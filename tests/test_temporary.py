"""Conditions that expire on their own (specification 7)."""

from __future__ import annotations

import pytest

from mashu import scopes, temporary
from mashu.errors import RefusedError

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


def test_a_banned_pattern_is_refused_here_too(cur):
    """The entrance check does not care which table the text was heading for."""
    with pytest.raises(RefusedError):
        temporary.put_temporary(
            cur, content="use the SECRETMARKER7 path this week", actor="agent", days=1
        )
    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 0
