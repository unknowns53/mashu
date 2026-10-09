from __future__ import annotations

from datetime import UTC, datetime

from mashu import config, ledger, scopes, traces

DERIVED = "the build cache lives under the state directory and survives a clean"
AGAIN = "the build cache lives under the state directory and survives a clean run"
UNRELATED = "midnight quotas apply to every shared queue on the cluster"


def test_a_trace_expires_on_its_own_and_writing_it_again_points_at_pain_report(cur):
    first = traces.put_trace(cur, content=DERIVED, actor="agent")
    ahead = first["expires_at"] - datetime.now(UTC)
    assert abs(ahead.total_seconds() - config.trace_ttl_days() * 86400) < 120
    assert first["similar"] == []
    assert first["note"] is None

    got = traces.put_trace(cur, content=AGAIN, actor="agent")
    assert [row["content"] for row in got["similar"]] == [DERIVED]
    assert got["similar"][0]["score"] >= config.match_threshold()
    assert "pain_report" in got["note"]

    cur.execute("UPDATE trace SET expires_at = now() - INTERVAL '1 day'")
    assert traces.search_traces(cur) == []
    assert traces.search_traces(cur, query=DERIVED) == []


def test_the_search_can_be_narrowed_to_a_scope_without_losing_the_general_ones(cur, scope_id):
    traces.put_trace(cur, content=DERIVED, actor="agent", scope_id=scope_id)
    traces.put_trace(cur, content=UNRELATED, actor="agent")

    elsewhere = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]
    here = [row["content"] for row in traces.search_traces(cur, scope_id=scope_id)]
    there = [row["content"] for row in traces.search_traces(cur, scope_id=elsewhere)]

    assert set(here) == {DERIVED, UNRELATED}
    assert set(there) == {UNRELATED}


def test_a_friction_that_matches_a_live_trace_freezes_it(cur):
    put = traces.put_trace(cur, content=DERIVED, actor="agent")
    cur.execute("UPDATE trace SET created_at = now() - INTERVAL '3 days'")
    got = ledger.report_pain(
        cur,
        kind="friction",
        what="worked it out a second time",
        prevention=AGAIN,
        actor="agent",
    )

    cur.execute("SELECT * FROM ledger WHERE from_trace IS NOT NULL")
    frozen = cur.fetchone()
    assert frozen["kind"] == "friction"
    assert frozen["prevention"] == DERIVED
    cur.execute("SELECT (now() - INTERVAL '3 days')::date AS day")
    assert f"first observed {cur.fetchone()['day'].isoformat()}" in frozen["what"]
    cur.execute("SELECT * FROM event_log WHERE event_type = 'trace_frozen'")
    event = cur.fetchone()
    assert (event["ledger_id"], event["trace_id"]) == (frozen["ledger_id"], put["trace_id"])

    nomination = got["nomination"]
    assert nomination["kind"] == "rederivation"
    assert nomination["evidence"] == [frozen["ledger_id"], got["ledger_id"]]
