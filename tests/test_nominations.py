from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest

from conftest import retire
from mashu import ledger, nominations, scopes
from mashu.errors import MashuError, RefusedError

HOLE = "always run the migration before starting the local server"
SAME_HOLE = "always run the migrations before starting the local server"
OTHER_HOLE = "quotas on the shared queue reset at midnight every day"
REMEMBER = {
    "kind": "user_instruction",
    "instruction": "Remember to run the migration before starting the local server",
    "conversation_ref": "conversation:turn-21",
}
ANYWAY = {"conflict_instruction": "Keep it anyway; the tool no longer refuses by itself"}


def admit(cur, nomination_id, **kwargs):
    cur.execute(
        "SELECT version, conflicts FROM nomination WHERE nomination_id = %s", (nomination_id,)
    )
    candidate = cur.fetchone()
    conflicts = nominations.conflict_rows(cur, candidate["conflicts"])
    acknowledged = [
        row["memory_id"]
        for row in conflicts
        if row.get("retirement_kind") in ("invalidated", "legacy")
    ]
    return nominations.admit(
        cur,
        nomination_id,
        expected_version=kwargs.pop("expected_version", candidate["version"]),
        approval={"kind": "user_direct", "conflict_ids": acknowledged},
        request_id=uuid4(),
        **{"actor": "user", "delivery": "always", **kwargs},
    )


def admit_on_instruction(cur, nomination, approval, *, version=None, request_id=None):
    return nominations.admit(
        cur,
        nomination["nomination_id"],
        actor="the agent",
        delivery="always",
        expected_version=version or nomination["version"],
        approval=approval,
        request_id=request_id or uuid4(),
    )


def count(cur, rows):
    return cur.execute(f"SELECT count(*) AS n FROM {rows}").fetchone()["n"]


def two_pains(cur, scope_id=None):
    """A rederivation candidate, standing on the two frictions that proved it."""
    first = ledger.report_pain(
        cur, kind="friction", what="looked it up", prevention=HOLE, actor="agent", scope_id=scope_id
    )
    second = ledger.report_pain(
        cur,
        kind="friction",
        what="looked it up again",
        prevention=SAME_HOLE,
        actor="agent",
        scope_id=scope_id,
    )
    return first, second, second["nomination"]


def retired_admission(cur):
    """An admitted rule for HOLE, since invalidated."""
    _, _, nomination = two_pains(cur)
    memory = admit(cur, nomination["nomination_id"])
    retire(cur, memory, "the tool refuses on its own now")
    return memory


def test_admitting_carries_the_evidence_across_and_names_where_the_rule_came_from(cur):
    first, second, nomination = two_pains(cur)
    memory = admit(cur, nomination["nomination_id"])

    assert memory["status"] == "active"
    assert memory["content"] == nomination["content"]
    assert memory["evidence"] == [str(first["ledger_id"]), str(second["ledger_id"])]

    cur.execute(
        "SELECT status, memory_id FROM nomination WHERE nomination_id = %s",
        (nomination["nomination_id"],),
    )
    row = cur.fetchone()
    assert row["status"] == "admitted"
    assert str(row["memory_id"]) == memory["memory_id"]

    cur.execute(
        "SELECT content, note FROM memory_revision WHERE memory_id = %s", (memory["memory_id"],)
    )
    revision = cur.fetchone()
    assert revision["content"] == nomination["content"]
    assert str(nomination["nomination_id"]) in revision["note"]


def test_an_admission_may_reword_the_rule_and_choose_where_it_lands(cur, scope_id):
    _, _, nomination = two_pains(cur, scope_id=scope_id)
    memory = admit(
        cur,
        nomination["nomination_id"],
        delivery="always",
        content="run the migration first",
    )
    assert memory["content"] == "run the migration first"
    assert memory["delivery"] == "always"
    assert memory["scope_id"] == str(scope_id)


def test_a_pending_candidate_can_be_reworded_without_being_decided(cur):
    _, _, nomination = two_pains(cur)
    reworded = "run the migration before the server"
    revised = nominations.revise(cur, nomination["nomination_id"], content=reworded, actor="user")

    assert revised["content"] == reworded
    assert revised["status"] == "pending"
    assert revised["evidence"] == nomination["evidence"]
    cur.execute("SELECT detail FROM event_log WHERE event_type = 'nomination_revised'")
    assert cur.fetchone()["detail"] == {
        "from_chars": len(nomination["content"]),
        "to_chars": len(reworded),
        "conflicts": [],
        "version": revised["version"],
    }


def test_a_decision_is_made_once_and_closes_the_candidate_to_every_other_change(cur):
    _, _, nomination = two_pains(cur)
    nomination_id = nomination["nomination_id"]
    admit(cur, nomination_id)
    assert nominations.pending_nominations(cur) == []
    with pytest.raises(MashuError, match="already admitted"):
        admit(cur, nomination_id)
    with pytest.raises(MashuError, match="already admitted"):
        nominations.decline(cur, nomination_id, actor="user", reason="no")
    with pytest.raises(MashuError, match="already admitted"):
        nominations.revise(cur, nomination_id, content="too late to change this", actor="user")
    with pytest.raises(MashuError, match=nominations.NOT_PENDING):
        nominations.defer(cur, nomination_id, actor="user", reason="too late")


def test_declining_needs_a_reason_because_the_pain_will_come_back(cur):
    _, _, nomination = two_pains(cur)
    with pytest.raises(MashuError, match="reason"):
        nominations.decline(cur, nomination["nomination_id"], actor="user", reason="")
    with pytest.raises(RefusedError):
        nominations.decline(cur, nomination["nomination_id"], actor="user", reason="SECRETMARKER9")

    assert len(nominations.pending_nominations(cur)) == 1
    declined = nominations.decline(
        cur, nomination["nomination_id"], actor="user", reason="the tool now refuses on its own"
    )
    assert declined["status"] == "declined"
    assert declined["decision_reason"] == "the tool now refuses on its own"
    assert nominations.pending_nominations(cur) == []


def test_putting_a_candidate_off_needs_a_reason_and_keeps_it_pending_behind_the_queue(cur):
    _, _, nomination = two_pains(cur)
    with pytest.raises(MashuError, match="reason"):
        nominations.defer(cur, nomination["nomination_id"], actor="user", reason="   ")
    with pytest.raises(RefusedError):
        nominations.defer(cur, nomination["nomination_id"], actor="user", reason="SECRETMARKER9")

    deferred = nominations.defer(
        cur, nomination["nomination_id"], actor="user", reason="the other team owns this call"
    )
    assert deferred["status"] == "pending"
    assert deferred["deferred_at"] is not None
    assert deferred["defer_reason"] == "the other team owns this call"
    assert len(nominations.pending_nominations(cur)) == 1
    assert nominations.pending_nominations(cur, include_deferred=False) == []
    assert nominations.deferred_count(cur) == 1

    again = nominations.defer(
        cur, nomination["nomination_id"], actor="user", reason="still waiting on them"
    )
    assert again["defer_reason"] == "still waiting on them"


def test_the_queue_hands_over_the_pains_and_not_their_ids(cur):
    first, second, _ = two_pains(cur)
    rows = nominations.pending_nominations(cur)[0]["evidence_rows"]

    assert [row["ledger_id"] for row in rows] == [first["ledger_id"], second["ledger_id"]]
    assert rows[0]["what"] == "looked it up"
    assert rows[1]["prevention"] == SAME_HOLE
    assert all(row["kind"] == "friction" for row in rows)


def test_an_unknown_kind_or_invented_evidence_is_refused_in_words(cur):
    with pytest.raises(MashuError, match="unknown nomination kind"):
        nominations.create_nomination(cur, content=HOLE, kind="a hunch", evidence=[], actor="agent")
    invented = uuid4()
    with pytest.raises(MashuError, match=str(invented)):
        nominations.create_nomination(
            cur, content=HOLE, kind="incident", evidence=[invented], actor="agent"
        )


def test_the_database_refuses_evidence_that_names_nothing_when_the_code_is_gone_round(
    cur, scope_id
):
    _, second, _ = two_pains(cur, scope_id=scope_id)
    with (
        pytest.raises(psycopg.errors.RaiseException, match="NULL element"),
        cur.connection.transaction(),
    ):
        cur.execute(
            "INSERT INTO memory (content, scope_id, delivery, evidence, created_by) "
            "VALUES (%s, %s, 'scope', %s, 'user')",
            (HOLE, scope_id, [second["ledger_id"], None]),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="do not exist"):
        cur.execute(
            "INSERT INTO nomination (content, scope_id, kind, evidence, created_by) "
            "VALUES (%s, %s, 'incident', %s, 'agent')",
            (HOLE, scope_id, [uuid4()]),
        )


# an instruction an agent says it was given (5.1, path three)
def test_a_carried_instruction_reaches_the_queue_once_and_stops_there(cur, scope_id):
    with pytest.raises(RefusedError):
        nominations.nominate_user_explicit(
            cur, content="write it under SECRETMARKER3 from now on", actor="agent"
        )
    assert count(cur, "ledger") == 0

    got = nominations.nominate_user_explicit(
        cur, content=HOLE, actor="the agent", scope_id=scope_id
    )
    nomination = got["nomination"]
    assert nomination["kind"] == "user_explicit"
    assert nomination["status"] == "pending"
    assert nomination["scope_id"] == scope_id
    assert nomination["evidence"] == [got["ledger_id"]]

    cur.execute("SELECT * FROM ledger WHERE ledger_id = %s", (got["ledger_id"],))
    evidence = cur.fetchone()
    assert evidence["kind"] == "claimed"
    assert evidence["prevention"] == HOLE
    assert "approval source" in evidence["what"]
    assert evidence["created_by"] == "the agent"
    assert count(cur, "memory") == 0

    second = nominations.nominate_user_explicit(
        cur, content=SAME_HOLE, actor="agent", scope_id=scope_id
    )
    assert second["nomination_existing"] is True
    assert second["nomination"]["nomination_id"] == nomination["nomination_id"]
    assert second["ledger_id"] is not None
    assert second["ledger_id"] in second["nomination"]["evidence"]
    assert len(second["nomination"]["evidence"]) == 2
    assert count(cur, "nomination WHERE status = 'pending'") == 1
    assert count(cur, "ledger WHERE kind = 'claimed'") == 2


def test_explicit_instruction_admits_in_the_same_session_and_keeps_agent_as_actor(cur):
    nomination = nominations.nominate_user_explicit(cur, content=HOLE, actor="the agent")[
        "nomination"
    ]
    request_id = uuid4()

    with pytest.raises(MashuError, match="approval kind"):
        admit_on_instruction(cur, nomination, {})
    memory = admit_on_instruction(cur, nomination, REMEMBER, request_id=request_id)
    retire(cur, memory, "the admitted rule later stopped applying", "out_of_scope")
    replay = admit_on_instruction(cur, nomination, REMEMBER, request_id=request_id)

    assert replay == memory
    assert replay["status"] == "active"
    assert memory["created_by"] == "the agent"
    cur.execute(
        "SELECT status, decided_by, approval_source FROM nomination WHERE nomination_id = %s",
        (nomination["nomination_id"],),
    )
    row = cur.fetchone()
    assert row["status"] == "admitted"
    assert row["decided_by"] == "the agent"
    assert row["approval_source"] == REMEMBER


@pytest.mark.parametrize(
    "field", ["instruction", "conversation_ref", "conflict_instruction", "reversal_instruction"]
)
def test_a_banned_pattern_in_an_instruction_quote_is_refused(cur, field):
    nomination = nominations.nominate_user_explicit(cur, content=HOLE, actor="the agent")[
        "nomination"
    ]
    approval = {**REMEMBER, field: "write SECRETMARKER9 into the memory"}
    with pytest.raises(RefusedError):
        admit_on_instruction(cur, nomination, approval)
    # A one-call remember raises the refusal instead of stopping at a candidate.
    with pytest.raises(RefusedError):
        nominations.remember_explicit(
            cur, content=OTHER_HOLE, actor="agent", approval=approval, request_id=uuid4()
        )
    assert count(cur, "memory") == 0
    cur.execute(
        "SELECT status FROM nomination WHERE nomination_id = %s", (nomination["nomination_id"],)
    )
    assert cur.fetchone()["status"] == "pending"


def test_admission_rejects_a_candidate_whose_content_or_scope_changed_after_reading(cur):
    nomination = nominations.nominate_user_explicit(
        cur, content="the original migration rule is signed before storage", actor="agent"
    )["nomination"]
    changed = nominations.revise(
        cur,
        nomination["nomination_id"],
        content="the current migration rule is verified before storage",
        actor="agent",
    )
    assert changed["version"] > nomination["version"]

    next_scope = scopes.create_scope(cur, name="a different candidate scope", actor="user")
    cur.execute(
        "UPDATE nomination SET scope_id = %s WHERE nomination_id = %s RETURNING version",
        (next_scope["scope_id"], nomination["nomination_id"]),
    )
    moved = cur.fetchone()["version"]
    assert moved > changed["version"]

    for stale in (nomination["version"], changed["version"]):
        with pytest.raises(MashuError, match="nomination version changed"):
            admit_on_instruction(cur, nomination, REMEMBER, version=stale)
    memory = admit_on_instruction(cur, nomination, REMEMBER, version=moved)
    assert memory["content"] == changed["content"]


def test_a_carried_instruction_reaches_the_queue_carrying_the_retirement_it_repeats(cur):
    memory = retired_admission(cur)

    got = nominations.nominate_user_explicit(cur, content=SAME_HOLE, actor="agent")
    assert got["tombstone_conflict"] is True
    assert got["nomination"]["status"] == "pending"
    assert got["ledger_id"] is not None
    assert got["nomination"]["conflicts"] == [UUID(memory["memory_id"])]
    assert got["matches"]["tombstones"][0]["retire_reason"] == "the tool refuses on its own now"

    # And it is on the queue with the reason resolved, not just an id.
    waiting = nominations.pending_nominations(cur)
    assert [row["retire_reason"] for row in waiting[0]["conflict_rows"]] == [
        "the tool refuses on its own now"
    ]
    # Never the withdrawn body itself: a tombstone answers with its reason.
    assert all("content" not in row for row in waiting[0]["conflict_rows"])


def test_evidence_is_added_once_and_never_to_a_candidate_already_decided(cur):
    first = ledger.report_pain(
        cur, kind="incident", what="wrong path", prevention=HOLE, actor="agent"
    )
    nomination_id = first["nomination"]["nomination_id"]
    once = nominations.add_evidence(cur, nomination_id, first["ledger_id"], actor="agent")
    assert once["evidence"] == [first["ledger_id"]]
    assert count(cur, "event_log WHERE event_type = 'nomination_evidence_added'") == 0

    admit(cur, nomination_id)
    later = ledger.report_pain(
        cur, kind="friction", what="looked it up", prevention=OTHER_HOLE, actor="agent"
    )
    assert nominations.add_evidence(cur, nomination_id, later["ledger_id"], actor="agent") is None


# an explicit remember carried in one call
def test_one_call_nominates_and_admits_with_the_same_provenance_and_replays_its_answer(
    cur, scope_id
):
    request_id = uuid4()
    got = nominations.remember_explicit(
        cur,
        content=HOLE,
        actor="the agent",
        approval=REMEMBER,
        request_id=request_id,
        scope_id=scope_id,
    )

    assert got["admitted"] is True
    memory = got["memory"]
    assert memory["content"] == HOLE
    assert memory["created_by"] == "the agent"
    assert memory["delivery"] == "scope"
    assert memory["scope_id"] == str(scope_id)
    assert memory["evidence"] == [str(got["ledger_id"])]

    cur.execute("SELECT * FROM nomination WHERE nomination_id = %s", (got["nomination_id"],))
    row = cur.fetchone()
    assert row["kind"] == "user_explicit"
    assert row["status"] == "admitted"
    assert row["decided_by"] == "the agent"
    assert row["admit_request_id"] == request_id
    assert row["approval_source"] == REMEMBER
    cur.execute("SELECT kind, prevention FROM ledger WHERE ledger_id = %s", (got["ledger_id"],))
    assert cur.fetchone() == {"kind": "claimed", "prevention": HOLE}

    retire(cur, memory, "the admitted rule later stopped applying", "out_of_scope")
    again = nominations.remember_explicit(
        cur,
        content=HOLE,
        actor="agent",
        approval=REMEMBER,
        request_id=request_id,
        scope_id=scope_id,
    )
    assert again == got
    assert count(cur, "nomination") == 1
    assert count(cur, "ledger") == 1

    with pytest.raises(MashuError, match="request_id was already used"):
        nominations.remember_explicit(
            cur, content=OTHER_HOLE, actor="agent", approval=REMEMBER, request_id=request_id
        )


def test_a_one_call_remember_stops_at_a_pending_candidate_it_has_not_read(cur):
    waiting = nominations.nominate_user_explicit(cur, content=HOLE, actor="agent")["nomination"]

    got = nominations.remember_explicit(
        cur, content=SAME_HOLE, actor="agent", approval=REMEMBER, request_id=uuid4()
    )

    assert got["admitted"] is False
    assert "pending candidate" in got["stopped"]
    assert got["nomination_existing"] is True
    assert got["nomination"]["nomination_id"] == waiting["nomination_id"]
    assert got["nomination"]["content"] == HOLE
    assert got["ledger_id"] in got["nomination"]["evidence"]
    assert count(cur, "memory") == 0
    cur.execute(
        "SELECT status FROM nomination WHERE nomination_id = %s", (waiting["nomination_id"],)
    )
    assert cur.fetchone()["status"] == "pending"


def test_a_one_call_remember_stops_at_an_unaddressed_invalidated_conflict(cur):
    retired = retired_admission(cur)

    got = nominations.remember_explicit(
        cur, content=SAME_HOLE, actor="agent", approval=REMEMBER, request_id=uuid4()
    )

    assert got["admitted"] is False
    assert "invalidated or legacy" in got["stopped"]
    assert got["nomination_existing"] is False
    assert got["nomination"]["status"] == "pending"
    assert got["nomination"]["conflicts"] == [UUID(retired["memory_id"])]
    assert count(cur, "memory WHERE status = 'active'") == 0
    cur.execute(
        "SELECT status, admit_request_id FROM nomination WHERE nomination_id = %s",
        (got["nomination"]["nomination_id"],),
    )
    assert cur.fetchone() == {"status": "pending", "admit_request_id": None}

    # The stopped candidate continues through the admission by id, unchanged.
    approval = {**REMEMBER, "conflict_ids": [retired["memory_id"]], **ANYWAY}
    assert admit_on_instruction(cur, got["nomination"], approval)["content"] == SAME_HOLE


def test_a_one_call_remember_admits_through_a_conflict_it_already_addresses(cur):
    retired = retired_admission(cur)
    got = nominations.remember_explicit(
        cur,
        content=SAME_HOLE,
        actor="agent",
        approval={**REMEMBER, "conflict_ids": [retired["memory_id"]], **ANYWAY},
        request_id=uuid4(),
    )
    assert got["admitted"] is True
    assert [row["memory_id"] for row in got["conflicts"]] == [retired["memory_id"]]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"nomination_id": str(uuid4()), "nomination_version": 1, "content": HOLE}, "exactly one"),
        ({}, "exactly one"),
        ({"nomination_id": str(uuid4())}, "nomination_version"),
        ({"content": HOLE, "nomination_version": 3}, "nomination_version"),
    ],
)
def test_memory_admit_takes_one_of_a_nomination_at_its_version_or_content(
    monkeypatch, arguments, message
):
    pytest.importorskip("mcp.server")
    import asyncio

    from mashu import server

    # Argument checks answer before any connection; a missing database proves it.
    monkeypatch.setenv("MASHU_DATABASE_URL", "dbname=mashu_test_never_created")
    base = {"request_id": str(uuid4()), "approval_kind": "user_instruction", "instruction": "r"}
    result = asyncio.run(server.build_server().call_tool("memory_admit", {**base, **arguments}))
    assert result.structured_content["ok"] is False
    assert message in result.structured_content["error"]
