"""The two stores that are not Memory, and the ledger (25.1, 25.2, 16.3)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from mashu import bootstrap, context, runs, scratch, worker
from mashu.db import transaction
from mashu.errors import NotFoundError
from mashu.models import SourceType


def soon(**kw):
    return datetime.now(UTC) + timedelta(**kw)


def _expire(cur):
    """Move a window wholly into the past. The check constraint holds either way."""
    cur.execute(
        "UPDATE temporary_context SET valid_from = now() - interval '2 days', "
        "expires_at = now() - interval '1 day'"
    )


def _run_until_it_gives_up(cur, run):
    """Drive one run through its retries, letting each backoff elapse."""
    states = []
    while True:
        cur.execute("UPDATE extraction_run SET next_retry_at = now() - interval '1 minute'")
        if not runs.claim(cur):
            break
        states.append(runs.failed(cur, run_id=run["run_id"], error="the model timed out")["state"])
        if states[-1] == "failed":
            break
    return states


@pytest.fixture
def session_id(cur):
    cur.execute("INSERT INTO agent_session (agent) VALUES ('claude') RETURNING session_id")
    return cur.fetchone()["session_id"]


# --------------------------------------------------------------------------
# temporary context: it leaves by the clock, with nothing running
# --------------------------------------------------------------------------
def test_a_condition_applies_until_its_moment_and_then_does_not(cur, scope_id):
    context.put(
        cur,
        content="the cluster is in maintenance",
        expires_at=soon(hours=2),
        kind="fact",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        scope_id=scope_id,
    )
    assert len(context.live(cur, scopes=[scope_id])) == 1

    # Nothing moves it. The row is untouched; only now() has changed.
    _expire(cur)
    assert context.live(cur, scopes=[scope_id]) == []


def test_expiry_needs_no_worker_to_have_run(cur, scope_id):
    """The argument that decided against putting an expiry on the version."""
    context.put(
        cur,
        content="already over",
        expires_at=soon(hours=1),
        kind="fact",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
    )
    _expire(cur)
    cur.execute("SELECT count(*) AS n FROM temporary_context")
    assert cur.fetchone()["n"] == 1  # still there, in the history
    assert context.live(cur) == []  # and not in the answer


def test_a_scopeless_condition_reaches_every_scope(cur, scope_id):
    context.put(
        cur,
        content="the delegation quota is free until tomorrow",
        expires_at=soon(hours=12),
        kind="fact",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
    )
    assert len(context.live(cur, scopes=[scope_id])) == 1


def test_an_agent_may_not_claim_a_window_longer_than_the_cap(cur):
    with pytest.raises(context.ContextError, match="belongs in a proposal"):
        context.put(
            cur,
            content="this holds for the rest of the year",
            expires_at=soon(days=context.AGENT_MAX_WINDOW_DAYS + 1),
            kind="fact",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
        )


def test_a_person_may(cur):
    row = context.put(
        cur,
        content="the visiting arrangement runs to the end of March",
        expires_at=soon(days=200),
        kind="fact",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
    )
    assert row["source_type"] == str(SourceType.USER)


def test_revoking_ends_it_early(cur):
    row = context.put(
        cur,
        content="the cluster is in maintenance",
        expires_at=soon(days=3),
        kind="fact",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
    )
    context.revoke(cur, context_id=row["context_id"], actor="user", reason="it came back early")
    assert context.live(cur) == []


def test_the_kinds_are_deliberately_few(cur):
    with pytest.raises(context.ContextError, match="design change"):
        context.put(
            cur,
            content="anything at all",
            expires_at=soon(hours=1),
            kind="observation",
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
        )


# --------------------------------------------------------------------------
# the session start carries the conditions, but not the ones an agent wrote
# --------------------------------------------------------------------------
def test_the_session_start_carries_what_a_person_stated(cur, scope_id):
    context.put(
        cur,
        content="the quota resets in the morning",
        expires_at=soon(hours=8),
        kind="fact",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
    )
    got = bootstrap.session_bootstrap(cur, actor="claude", record_event=False)
    assert [row["content"] for row in got.temporary] == ["the quota resets in the morning"]


def test_what_an_agent_wrote_is_not_pushed(cur, scope_id):
    """25.2 separates the right to write from the right to be pushed.

    A session start reaches every session, including the ones that never asked,
    so a wrong window there costs more than the same window in a search result.
    """
    context.put(
        cur,
        content="the build host is down until Friday",
        expires_at=soon(days=2),
        kind="fact",
        source_type=SourceType.AGENT,
        created_by="claude",
        actor="claude",
    )
    got = bootstrap.session_bootstrap(cur, actor="claude", record_event=False)
    assert got.temporary == []
    assert len(context.live(cur)) == 1  # reachable by whoever looks


def test_the_session_start_says_whether_capture_is_working(cur):
    got = bootstrap.session_bootstrap(cur, actor="claude", record_event=False)
    assert got.health["ok"] is True

    run = runs.enqueue(
        cur,
        source_cli="claude",
        external_session_id="s-1",
        transcript_digest="d-1",
        extractor_version="v1",
    )
    _run_until_it_gives_up(cur, run)

    got = bootstrap.session_bootstrap(cur, actor="claude", record_event=False)
    assert got.health["ok"] is False
    assert "gave up" in got.health["warning"]


# --------------------------------------------------------------------------
# scratch: one session, and it is the extraction's first input
# --------------------------------------------------------------------------
def test_scratch_holds_items_not_a_blob(cur, session_id):
    scratch.put(cur, session_id=session_id, content="the guard is in, sweep not rerun")
    scratch.put(
        cur, session_id=session_id, content="probe echo line looked wrong", kind="candidate"
    )

    held = scratch.get(cur, session_id=session_id)
    assert [item["kind"] for item in held] == ["note", "candidate"]
    assert all(item["item_id"] for item in held)


def test_scratch_belongs_to_one_session(cur, session_id):
    scratch.put(cur, session_id=session_id, content="mine")
    cur.execute("INSERT INTO agent_session (agent) VALUES ('codex') RETURNING session_id")
    other = cur.fetchone()["session_id"]
    assert scratch.get(cur, session_id=other) == []


def test_scratch_is_cleared_only_after_extraction_succeeds(cur, session_id):
    scratch.put(cur, session_id=session_id, content="worth keeping")
    assert scratch.clear(cur, session_id=session_id) == 1
    assert scratch.get(cur, session_id=session_id) == []


def test_a_session_that_does_not_exist_is_an_error(cur):
    import uuid

    with pytest.raises(NotFoundError):
        scratch.put(cur, session_id=uuid.uuid4(), content="nowhere")


def test_the_external_session_is_recorded_so_a_retry_is_idempotent(cur, session_id):
    scratch.register(cur, session_id=session_id, source_cli="claude", external_session_id="abc-123")
    cur.execute(
        "SELECT source_cli, external_session_id FROM agent_session WHERE session_id = %s",
        (session_id,),
    )
    row = cur.fetchone()
    assert (row["source_cli"], row["external_session_id"]) == ("claude", "abc-123")


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------
def test_the_same_transcript_is_claimed_once(cur):
    args = dict(
        source_cli="claude",
        external_session_id="s-9",
        transcript_digest="d-9",
        extractor_version="v1",
    )
    first = runs.enqueue(cur, **args)
    again = runs.enqueue(cur, **args)
    assert first["run_id"] == again["run_id"]

    cur.execute("SELECT count(*) AS n FROM extraction_run")
    assert cur.fetchone()["n"] == 1


def test_a_failure_retries_and_then_stops(cur):
    run = runs.enqueue(
        cur,
        source_cli="claude",
        external_session_id="s-2",
        transcript_digest="d-2",
        extractor_version="v1",
    )
    states = _run_until_it_gives_up(cur, run)

    assert states[-1] == "failed"
    assert states[:-1] == ["retrying"] * (runs.MAX_ATTEMPTS - 1)
    assert len(states) == runs.MAX_ATTEMPTS


def test_a_skip_is_recorded_rather_than_dropped(cur):
    """A skip nobody can see reads the same as a silent failure."""
    run = runs.enqueue(
        cur,
        source_cli="codex",
        external_session_id="s-3",
        transcript_digest="d-3",
        extractor_version="v1",
    )
    runs.claim(cur)
    row = runs.skipped(cur, run_id=run["run_id"], note="under the size floor, scratch empty")
    assert row["state"] == "skipped"
    assert runs.health(cur)["skipped"] == 1


def test_claiming_takes_the_oldest_first(cur):
    for i in range(3):
        runs.enqueue(
            cur,
            source_cli="claude",
            external_session_id=f"s-{i}",
            transcript_digest=f"d-{i}",
            extractor_version="v1",
        )
    taken = runs.claim(cur, limit=2)
    assert [r["external_session_id"] for r in taken] == ["s-0", "s-1"]
    assert all(r["state"] == "running" for r in taken)


def test_health_is_quiet_when_there_is_nothing_wrong(cur):
    runs.enqueue(
        cur,
        source_cli="claude",
        external_session_id="s-ok",
        transcript_digest="d-ok",
        extractor_version="v1",
    )
    run = runs.claim(cur)[0]
    runs.succeeded(cur, run_id=run["run_id"], model="small", input_tokens=9000)
    state = runs.health(cur)
    assert state["ok"] is True
    assert state["warning"] is None


def test_a_sweeper_that_stopped_is_the_one_failure_counting_rows_cannot_see(cur):
    """16.3 asks whether capture is still working, and rows cannot answer it.

    If the hook stops enqueueing and the sweeper stops running, the lost
    sessions are in neither place. Nothing failed, nothing is waiting, and
    capture is dead. That is the state a year of nobody attending reaches
    quietly, so the last time the sweeper spoke is part of the reading.
    """
    assert runs.health(cur)["ok"] is True, "never having swept is a fresh install"

    runs.mark_swept(cur, source_cli="claude", swept_to=datetime.now(UTC), files_seen=3)
    got = runs.health(cur)
    assert got["ok"] is True and got["swept_hours_ago"] is not None

    cur.execute(
        "UPDATE sweep_watermark SET swept_at = now() - make_interval(hours => %s)",
        (runs.SWEEP_SILENT_HOURS + 1,),
    )
    got = runs.health(cur)
    assert got["ok"] is False
    assert got["warning"]


def test_the_sweep_walks_from_where_it_got_to_rather_than_a_fixed_window(committing_dsn, tmp_path):
    """A window promises nothing goes wrong for longer than the window.

    The failure it guards against is the one that lasts longer than that: the
    hook stops enqueueing, nobody notices for a fortnight, and by the time the
    sweeper is asked the transcripts have fallen out the back of its reach.
    """
    root = tmp_path / "claude"
    root.mkdir()
    old = root / "old.jsonl"
    old.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "long-ago",
                "cwd": "/work/x",
                "message": {"content": "問"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    long_ago = (datetime.now() - timedelta(days=90)).timestamp()
    os.utime(old, (long_ago, long_ago))
    roots = {"claude": str(root)}

    with transaction(committing_dsn) as cur:
        cur.execute("DELETE FROM sweep_watermark WHERE source_cli = 'claude'")

    # With no mark, the fixed reach decides, and a file this old is past it.
    assert worker.sweep(committing_dsn, roots=roots, since_days=7) == []

    # The sweep still leaves a mark, because a quiet sweep is evidence too.
    with transaction(committing_dsn) as cur:
        assert runs.swept_to(cur, "claude") is not None
        cur.execute(
            "UPDATE sweep_watermark SET swept_to = now() - make_interval(days => 120) "
            "WHERE source_cli = 'claude'"
        )

    found = worker.sweep(committing_dsn, roots=roots, since_days=7)
    assert [row["external_session_id"] for row in found] == ["long-ago"]

    with transaction(committing_dsn) as cur:
        cur.execute("DELETE FROM extraction_run WHERE external_session_id = 'long-ago'")
        cur.execute("DELETE FROM sweep_watermark WHERE source_cli = 'claude'")
