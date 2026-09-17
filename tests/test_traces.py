
from __future__ import annotations

from datetime import UTC, datetime

from mashu import config, ledger, scopes, traces

DERIVED = "the build cache lives under the state directory and survives a clean"
AGAIN = "the build cache lives under the state directory and survives a clean run"
UNRELATED = "midnight quotas apply to every shared queue on the cluster"


def test_a_trace_expires_on_its_own(cur):
    got = traces.put_trace(cur, content=DERIVED, actor="agent")
    ahead = got["expires_at"] - datetime.now(UTC)
    assert abs(ahead.total_seconds() - config.trace_ttl_days() * 86400) < 120


def test_writing_the_same_thing_again_is_pointed_at_the_way_to_make_it_count(cur):
    traces.put_trace(cur, content=DERIVED, actor="agent")
    got = traces.put_trace(cur, content=AGAIN, actor="agent")

    assert [row["content"] for row in got["similar"]] == [DERIVED]
    assert got["similar"][0]["score"] >= config.match_threshold()
    assert "pain_report" in got["note"]


def test_a_first_trace_has_nothing_to_say(cur):
    got = traces.put_trace(cur, content=DERIVED, actor="agent")
    assert got["similar"] == []
    assert got["note"] is None


def test_an_expired_trace_is_gone_from_the_search(cur):
    traces.put_trace(cur, content=DERIVED, actor="agent")
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
    traces.put_trace(cur, content=DERIVED, actor="agent")
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
    assert "first observed" in frozen["what"]

    nomination = got["nomination"]
    assert nomination["kind"] == "rederivation"
    assert nomination["evidence"] == [frozen["ledger_id"], got["ledger_id"]]


def test_freezing_records_both_ends(cur):
    put = traces.put_trace(cur, content=DERIVED, actor="agent")
    cur.execute("SELECT * FROM trace WHERE trace_id = %s", (put["trace_id"],))
    row = traces.freeze_trace(cur, cur.fetchone(), actor="agent")

    cur.execute("SELECT * FROM event_log WHERE event_type = 'trace_frozen'")
    event = cur.fetchone()
    assert event["ledger_id"] == row["ledger_id"]
    assert event["trace_id"] == put["trace_id"]


def test_a_trace_written_yesterday_says_when_it_was_derived(cur):
    put = traces.put_trace(cur, content=DERIVED, actor="agent")
    cur.execute(
        "UPDATE trace SET created_at = now() - INTERVAL '3 days' WHERE trace_id = %s",
        (put["trace_id"],),
    )
    cur.execute("SELECT * FROM trace WHERE trace_id = %s", (put["trace_id"],))
    trace = cur.fetchone()
    frozen = traces.freeze_trace(cur, trace, actor="agent")

    cur.execute("SELECT (now() - INTERVAL '3 days')::date AS day")
    expected = cur.fetchone()["day"].isoformat()
    assert expected in frozen["what"]
