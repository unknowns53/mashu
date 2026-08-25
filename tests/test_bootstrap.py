"""What a session opens with (specification 6.1)."""

from __future__ import annotations

from mashu import bootstrap, ledger, memories, scopes, temporary
from mashu.tokens import pushed_cost

DISCIPLINE = "never report a run as finished without the output that proves it"
LOCAL = "the migration runs before the local server starts"
PINNED = "delegation goes to the reviewer, not to another writer"


def test_a_rule_for_every_session_reaches_every_session(cur, scope_id):
    memories.remember(cur, content=DISCIPLINE, actor="user")
    blind = bootstrap.session_bootstrap(cur, actor="agent")
    scoped = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)

    assert [row["content"] for row in blind["always"]] == [DISCIPLINE]
    assert [row["content"] for row in scoped["always"]] == [DISCIPLINE]


def test_a_scope_rule_waits_for_a_session_in_that_scope(cur, scope_id):
    """Sending a scope's rules everywhere dilutes the layer everyone does read."""
    memories.remember(cur, content=LOCAL, actor="user", scope_id=scope_id, delivery="scope")
    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]

    assert bootstrap.session_bootstrap(cur, actor="agent")["scoped"] == []
    assert bootstrap.session_bootstrap(cur, actor="agent", scope_id=elsewhere)["scoped"] == []

    here = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert [row["content"] for row in here["scoped"]] == [LOCAL]


def test_a_rule_held_at_the_act_gate_is_not_in_the_opening(cur, scope_id):
    """That is the point of moving it there: it arrives at the decision, not before it."""
    memories.remember(
        cur,
        content=PINNED,
        actor="user",
        scope_id=scope_id,
        delivery="guard",
        guard_action="Task",
    )
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert got["always"] == [] and got["scoped"] == []
    assert got["tokens"] == 0


def test_a_pushed_row_carries_an_id_and_a_body_and_nothing_else(cur):
    """Scaffolding in a fixed-size opening evicts the rules it surrounds."""
    memories.remember(cur, content=DISCIPLINE, actor="user")
    row = bootstrap.session_bootstrap(cur, actor="agent")["always"][0]
    assert set(row) == {"memory_id", "content"}


def test_the_conditions_of_the_week_come_with_the_opening(cur, scope_id):
    temporary.put_temporary(cur, content="the licence server is offline", actor="user", days=3)
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert [row["content"] for row in got["temporary"]] == ["the licence server is offline"]
    assert set(got["temporary"][0]) == {"content", "expires_at"}


def test_a_candidate_waiting_is_worth_one_number(cur):
    """The person has to learn the queue is not empty somewhere, and this costs a line."""
    assert bootstrap.session_bootstrap(cur, actor="agent")["pending"] == 0

    ledger.report_pain(
        cur,
        kind="incident",
        what="the wrong branch was deployed",
        prevention=LOCAL,
        actor="agent",
    )
    assert bootstrap.session_bootstrap(cur, actor="agent")["pending"] == 1


def test_the_opening_reports_what_it_cost_and_whether_it_fits(cur, scope_id, monkeypatch):
    """Being over is a fact about the store, not a reason to serve less of it.

    Trimming would drop exactly the standing rules the session was opened
    with, so the opening says so and stays whole.
    """
    memories.remember(cur, content=DISCIPLINE, actor="user")
    memories.remember(cur, content=LOCAL, actor="user", scope_id=scope_id, delivery="scope")

    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert got["tokens"] == pushed_cost([DISCIPLINE, LOCAL])
    assert got["capacity"] == 2000
    assert got["over_budget"] is False

    monkeypatch.setenv("MASHU_CAPACITY", "10")
    tight = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert tight["capacity"] == 10
    assert tight["over_budget"] is True
    assert len(tight["always"]) == 1 and len(tight["scoped"]) == 1


def test_what_was_handed_over_is_logged(cur, scope_id):
    memories.remember(cur, content=DISCIPLINE, actor="user")
    got = bootstrap.session_bootstrap(
        cur, actor="agent", scope_id=scope_id, scope_name="test scope", routed=True
    )
    assert got["scope"] == "test scope"
    assert got["routed"] is True

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'bootstrap_served'")
    detail = cur.fetchone()["detail"]
    assert detail["tokens"] == got["tokens"]
    assert detail["scope"] == "test scope"
