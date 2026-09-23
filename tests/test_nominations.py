from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest

from mashu import ledger, memories, nominations, scopes
from mashu.errors import MashuError, RefusedError

HOLE = "always run the migration before starting the local server"
SAME_HOLE = "always run the migrations before starting the local server"
OTHER_HOLE = "quotas on the shared queue reset at midnight every day"


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
        **kwargs,
    )


def two_pains(cur, scope_id=None):
    """A rederivation candidate, standing on the two frictions that proved it."""
    first = ledger.report_pain(
        cur,
        kind="friction",
        what="looked it up",
        prevention=HOLE,
        actor="agent",
        scope_id=scope_id,
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


def test_admitting_carries_the_evidence_across(cur):
    first, second, nomination = two_pains(cur)
    memory = admit(cur, nomination["nomination_id"], actor="user", delivery="always")

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


def test_the_first_revision_names_where_the_rule_came_from(cur):
    _, _, nomination = two_pains(cur)
    memory = admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    cur.execute(
        "SELECT content, note FROM memory_revision WHERE memory_id = %s",
        (memory["memory_id"],),
    )
    row = cur.fetchone()
    assert row["content"] == nomination["content"]
    assert str(nomination["nomination_id"]) in row["note"]


def test_an_admission_may_reword_the_rule_and_choose_where_it_lands(cur, scope_id):
    _, _, nomination = two_pains(cur, scope_id=scope_id)
    memory = admit(
        cur,
        nomination["nomination_id"],
        actor="user",
        delivery="guard",
        guard_action="Bash",
        content="run the migration first",
    )
    assert memory["content"] == "run the migration first"
    assert memory["delivery"] == "guard"
    assert memory["guard_action"] == "Bash"
    assert memory["scope_id"] == str(scope_id)


def test_a_pending_candidate_can_be_reworded_without_being_decided(cur):
    _, _, nomination = two_pains(cur)
    revised = nominations.revise(
        cur,
        nomination["nomination_id"],
        content="run the migration before the server",
        actor="user",
    )

    assert revised["content"] == "run the migration before the server"
    assert revised["status"] == "pending"
    assert revised["evidence"] == nomination["evidence"]
    cur.execute("SELECT detail FROM event_log WHERE event_type = 'nomination_revised'")
    assert cur.fetchone()["detail"] == {
        "from_chars": len(nomination["content"]),
        "to_chars": len("run the migration before the server"),
        "conflicts": [],
        "version": revised["version"],
    }


def test_a_decided_candidate_cannot_be_reworded(cur):
    _, _, nomination = two_pains(cur)
    nominations.decline(cur, nomination["nomination_id"], actor="user", reason="obsolete")

    with pytest.raises(MashuError, match="already declined"):
        nominations.revise(
            cur,
            nomination["nomination_id"],
            content="too late to change this",
            actor="user",
        )


def test_a_decision_is_made_once(cur):
    _, _, nomination = two_pains(cur)
    admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    with pytest.raises(MashuError, match="already admitted"):
        admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    with pytest.raises(MashuError, match="already admitted"):
        nominations.decline(cur, nomination["nomination_id"], actor="user", reason="no")


def test_declining_needs_a_reason_because_the_pain_will_come_back(cur):
    _, _, nomination = two_pains(cur)
    with pytest.raises(MashuError, match="reason"):
        nominations.decline(cur, nomination["nomination_id"], actor="user", reason="")

    declined = nominations.decline(
        cur, nomination["nomination_id"], actor="user", reason="the tool now refuses on its own"
    )
    assert declined["status"] == "declined"
    assert declined["decision_reason"] == "the tool now refuses on its own"


def test_a_decided_candidate_leaves_the_queue(cur):
    _, _, nomination = two_pains(cur)
    assert len(nominations.pending_nominations(cur)) == 1

    nominations.decline(cur, nomination["nomination_id"], actor="user", reason="not worth a seat")
    assert nominations.pending_nominations(cur) == []

    other = nominations.create_nomination(
        cur,
        content="answer in the language that was asked",
        kind="user_explicit",
        evidence=[nomination["evidence"][0]],
        actor="agent",
    )
    admit(cur, other["nomination_id"], actor="user", delivery="always")
    assert nominations.pending_nominations(cur) == []


def test_putting_a_candidate_off_needs_a_reason_and_does_not_decide_it(cur):
    _, _, nomination = two_pains(cur)
    with pytest.raises(MashuError, match="reason"):
        nominations.defer(cur, nomination["nomination_id"], actor="user", reason="   ")

    deferred = nominations.defer(
        cur, nomination["nomination_id"], actor="user", reason="the other team owns this call"
    )
    assert deferred["status"] == "pending"
    assert deferred["deferred_at"] is not None
    assert deferred["defer_reason"] == "the other team owns this call"

    again = nominations.defer(
        cur, nomination["nomination_id"], actor="user", reason="still waiting on them"
    )
    assert again["defer_reason"] == "still waiting on them"


def test_a_candidate_put_off_still_counts_as_pending_but_stops_leading_the_queue(cur):
    _, _, nomination = two_pains(cur)
    nominations.defer(cur, nomination["nomination_id"], actor="user", reason="not this week")

    assert len(nominations.pending_nominations(cur)) == 1
    assert nominations.pending_nominations(cur, include_deferred=False) == []
    assert nominations.deferred_count(cur) == 1


def test_a_decided_candidate_cannot_be_put_off_afterwards(cur):
    _, _, nomination = two_pains(cur)
    nominations.decline(cur, nomination["nomination_id"], actor="user", reason="not worth a seat")
    with pytest.raises(MashuError, match=nominations.NOT_PENDING):
        nominations.defer(cur, nomination["nomination_id"], actor="user", reason="too late")


def test_the_queue_hands_over_the_pains_and_not_their_ids(cur):
    first, second, _ = two_pains(cur)
    waiting = nominations.pending_nominations(cur)[0]
    rows = waiting["evidence_rows"]

    assert [row["ledger_id"] for row in rows] == [first["ledger_id"], second["ledger_id"]]
    assert rows[0]["what"] == "looked it up"
    assert rows[1]["prevention"] == SAME_HOLE
    assert all(row["kind"] == "friction" for row in rows)


def test_a_kind_the_schema_does_not_know_is_refused_before_the_insert(cur):
    with pytest.raises(MashuError, match="unknown nomination kind"):
        nominations.create_nomination(cur, content=HOLE, kind="a hunch", evidence=[], actor="agent")


# an instruction an agent says it was given (5.1, path three)
def test_a_carried_instruction_reaches_the_queue_and_stops_there(cur, scope_id):
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

    cur.execute("SELECT count(*) AS n FROM memory")
    assert cur.fetchone()["n"] == 0


def test_explicit_instruction_admits_in_the_same_session_and_keeps_agent_as_actor(cur):
    got = nominations.nominate_user_explicit(cur, content=HOLE, actor="the agent")
    nomination = got["nomination"]
    nomination_id = nomination["nomination_id"]
    request_id = uuid4()
    approval = {
        "kind": "user_instruction",
        "instruction": "Remember to run the migration before starting the local server",
        "conversation_ref": "conversation:turn-18",
    }

    with pytest.raises(MashuError, match="approval kind"):
        nominations.admit(
            cur,
            nomination_id,
            actor="the agent",
            delivery="always",
            expected_version=nomination["version"],
            approval={},
            request_id=uuid4(),
        )
    memory = nominations.admit(
        cur,
        nomination_id,
        actor="the agent",
        delivery="always",
        expected_version=nomination["version"],
        approval=approval,
        request_id=request_id,
    )
    memories.retire(
        cur,
        memory["memory_id"],
        reason="the admitted rule later stopped applying",
        retirement_kind="out_of_scope",
        actor="user",
    )
    replay = nominations.admit(
        cur,
        nomination_id,
        actor="the agent",
        delivery="always",
        expected_version=nomination["version"],
        approval=approval,
        request_id=request_id,
    )

    assert replay == memory
    assert replay["status"] == "active"
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "retired"
    assert memory["created_by"] == "the agent"
    cur.execute(
        "SELECT status, decided_by, approval_source FROM nomination WHERE nomination_id = %s",
        (nomination_id,),
    )
    row = cur.fetchone()
    assert row["status"] == "admitted"
    assert row["decided_by"] == "the agent"
    assert row["approval_source"]["kind"] == "user_instruction"
    assert row["approval_source"]["instruction"] == approval["instruction"]
    assert row["approval_source"]["conversation_ref"] == approval["conversation_ref"]


def test_a_banned_pattern_in_an_instruction_quote_is_refused(cur):
    nomination = nominations.nominate_user_explicit(cur, content=HOLE, actor="the agent")[
        "nomination"
    ]
    with pytest.raises(RefusedError):
        nominations.admit(
            cur,
            nomination["nomination_id"],
            actor="the agent",
            delivery="always",
            expected_version=nomination["version"],
            approval={
                "kind": "user_instruction",
                "instruction": "write SECRETMARKER9 into the memory",
                "conversation_ref": "conversation:turn-19",
            },
            request_id=uuid4(),
        )
    cur.execute(
        "SELECT status FROM nomination WHERE nomination_id = %s",
        (nomination["nomination_id"],),
    )
    assert cur.fetchone()["status"] == "pending"


def test_admission_rejects_a_candidate_version_changed_after_reading(cur):
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

    with pytest.raises(MashuError, match="nomination version changed"):
        nominations.admit(
            cur,
            nomination["nomination_id"],
            actor="agent",
            delivery="always",
            expected_version=nomination["version"],
            approval={
                "kind": "user_instruction",
                "instruction": "Remember the original migration rule",
            },
            request_id=uuid4(),
        )

    memory = nominations.admit(
        cur,
        nomination["nomination_id"],
        actor="agent",
        delivery="always",
        expected_version=changed["version"],
        approval={
            "kind": "user_instruction",
            "instruction": "Remember the verified migration rule",
        },
        request_id=uuid4(),
    )
    assert memory["content"] == changed["content"]


def test_nomination_version_changes_when_scope_changes(cur, scope_id):
    nomination = nominations.nominate_user_explicit(
        cur,
        content="scope-bound checksum rule waits for its own context",
        actor="agent",
        scope_id=scope_id,
    )["nomination"]
    next_scope = scopes.create_scope(cur, name="a different candidate scope", actor="user")
    cur.execute(
        "UPDATE nomination SET scope_id = %s WHERE nomination_id = %s",
        (next_scope["scope_id"], nomination["nomination_id"]),
    )
    cur.execute(
        "SELECT version FROM nomination WHERE nomination_id = %s",
        (nomination["nomination_id"],),
    )
    assert cur.fetchone()["version"] > nomination["version"]


def test_the_same_instruction_carried_twice_queues_once_and_counts_twice(cur):
    first = nominations.nominate_user_explicit(cur, content=HOLE, actor="agent")
    second = nominations.nominate_user_explicit(cur, content=SAME_HOLE, actor="agent")

    assert second["nomination_existing"] is True
    assert second["nomination"]["nomination_id"] == first["nomination"]["nomination_id"]
    assert second["ledger_id"] is not None
    assert second["ledger_id"] in second["nomination"]["evidence"]
    assert len(second["nomination"]["evidence"]) == 2

    cur.execute("SELECT count(*) AS n FROM nomination WHERE status = 'pending'")
    assert cur.fetchone()["n"] == 1
    cur.execute("SELECT count(*) AS n FROM ledger WHERE kind = 'claimed'")
    assert cur.fetchone()["n"] == 2


def test_a_carried_instruction_reaches_the_queue_carrying_the_retirement_it_repeats(cur):
    _, _, nomination = two_pains(cur)
    memory = admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    withdrawn = "the tool refuses on its own now"
    memories.retire(
        cur, memory["memory_id"], reason=withdrawn, actor="user", retirement_kind="invalidated"
    )

    got = nominations.nominate_user_explicit(cur, content=SAME_HOLE, actor="agent")
    assert got["tombstone_conflict"] is True
    assert got["nomination"]["status"] == "pending"
    assert got["ledger_id"] is not None
    assert got["nomination"]["conflicts"] == [UUID(memory["memory_id"])]
    assert got["matches"]["tombstones"][0]["retire_reason"] == withdrawn

    # And it is on the queue with the reason resolved, not just an id.
    waiting = nominations.pending_nominations(cur)
    assert [row["retire_reason"] for row in waiting[0]["conflict_rows"]] == [withdrawn]
    # Never the withdrawn body itself: a tombstone answers with its reason.
    assert all("content" not in row for row in waiting[0]["conflict_rows"])


def test_a_banned_pattern_is_refused_before_anything_is_carried(cur):
    with pytest.raises(RefusedError):
        nominations.nominate_user_explicit(
            cur, content="write it under SECRETMARKER3 from now on", actor="agent"
        )
    cur.execute("SELECT count(*) AS n FROM ledger")
    assert cur.fetchone()["n"] == 0


# evidence that names nothing (FIX 7)
def test_evidence_naming_a_row_that_does_not_exist_is_refused_in_words(cur):
    invented = uuid4()
    with pytest.raises(MashuError, match=str(invented)):
        nominations.create_nomination(
            cur, content=HOLE, kind="incident", evidence=[invented], actor="agent"
        )


def test_the_database_refuses_the_same_thing_when_the_code_is_gone_round(cur, scope_id):
    with pytest.raises(psycopg.errors.RaiseException, match="do not exist"):
        cur.execute(
            "INSERT INTO nomination (content, scope_id, kind, evidence, created_by) "
            "VALUES (%s, %s, 'incident', %s, 'agent')",
            (HOLE, scope_id, [uuid4()]),
        )


def test_the_database_refuses_a_hole_in_the_evidence_array(cur, scope_id):
    _, second, _ = two_pains(cur, scope_id=scope_id)
    with pytest.raises(psycopg.errors.RaiseException, match="NULL element"):
        cur.execute(
            "INSERT INTO memory (content, scope_id, delivery, evidence, created_by) "
            "VALUES (%s, %s, 'scope', %s, 'user')",
            (HOLE, scope_id, [second["ledger_id"], None]),
        )


def test_adding_the_same_pain_twice_does_not_lengthen_the_evidence(cur):
    first = ledger.report_pain(
        cur, kind="incident", what="wrong path", prevention=HOLE, actor="agent"
    )
    nomination_id = first["nomination"]["nomination_id"]

    once = nominations.add_evidence(cur, nomination_id, first["ledger_id"], actor="agent")
    assert once["evidence"] == [first["ledger_id"]]

    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = 'nomination_evidence_added'"
    )
    assert cur.fetchone()["n"] == 0


def test_evidence_is_not_added_to_a_candidate_somebody_already_decided(cur):
    _, _, nomination = two_pains(cur)
    admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    later = ledger.report_pain(
        cur, kind="friction", what="looked it up", prevention=OTHER_HOLE, actor="agent"
    )

    assert (
        nominations.add_evidence(
            cur, nomination["nomination_id"], later["ledger_id"], actor="agent"
        )
        is None
    )


# an explicit remember carried in one call
REMEMBER = {
    "kind": "user_instruction",
    "instruction": "Remember to run the migration before starting the local server",
    "conversation_ref": "conversation:turn-21",
}


def test_content_is_nominated_and_admitted_in_one_call_with_the_same_provenance(cur, scope_id):
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
    assert row["approval_source"] == {
        "kind": "user_instruction",
        "instruction": REMEMBER["instruction"],
        "conversation_ref": REMEMBER["conversation_ref"],
    }
    cur.execute("SELECT kind, prevention FROM ledger WHERE ledger_id = %s", (got["ledger_id"],))
    assert cur.fetchone() == {"kind": "claimed", "prevention": HOLE}


def test_a_one_call_remember_replayed_returns_the_first_response(cur):
    request_id = uuid4()
    first = nominations.remember_explicit(
        cur, content=HOLE, actor="agent", approval=REMEMBER, request_id=request_id
    )
    memories.retire(
        cur,
        first["memory"]["memory_id"],
        reason="the admitted rule later stopped applying",
        retirement_kind="out_of_scope",
        actor="user",
    )
    again = nominations.remember_explicit(
        cur, content=HOLE, actor="agent", approval=REMEMBER, request_id=request_id
    )

    assert again == first
    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 1
    cur.execute("SELECT count(*) AS n FROM ledger")
    assert cur.fetchone()["n"] == 1

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
    cur.execute("SELECT count(*) AS n FROM memory")
    assert cur.fetchone()["n"] == 0
    cur.execute(
        "SELECT status FROM nomination WHERE nomination_id = %s", (waiting["nomination_id"],)
    )
    assert cur.fetchone()["status"] == "pending"


def test_a_one_call_remember_stops_at_an_unaddressed_invalidated_conflict(cur):
    _, _, nomination = two_pains(cur)
    retired = admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    memories.retire(
        cur,
        retired["memory_id"],
        reason="the tool refuses on its own now",
        actor="user",
        retirement_kind="invalidated",
    )

    got = nominations.remember_explicit(
        cur, content=SAME_HOLE, actor="agent", approval=REMEMBER, request_id=uuid4()
    )

    assert got["admitted"] is False
    assert "invalidated or legacy" in got["stopped"]
    assert got["nomination_existing"] is False
    assert got["nomination"]["status"] == "pending"
    assert got["nomination"]["conflicts"] == [UUID(retired["memory_id"])]
    cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
    assert cur.fetchone()["n"] == 0
    cur.execute(
        "SELECT status, admit_request_id FROM nomination WHERE nomination_id = %s",
        (got["nomination"]["nomination_id"],),
    )
    assert cur.fetchone() == {"status": "pending", "admit_request_id": None}

    # The stopped candidate continues through the admission by id, unchanged.
    memory = nominations.admit(
        cur,
        got["nomination"]["nomination_id"],
        actor="agent",
        delivery="always",
        expected_version=got["nomination"]["version"],
        approval={
            **REMEMBER,
            "conflict_ids": [retired["memory_id"]],
            "conflict_instruction": "Keep it anyway; the tool no longer refuses by itself",
        },
        request_id=uuid4(),
    )
    assert memory["content"] == SAME_HOLE


def test_a_one_call_remember_admits_through_a_conflict_it_already_addresses(cur):
    _, _, nomination = two_pains(cur)
    retired = admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    memories.retire(
        cur,
        retired["memory_id"],
        reason="the tool refuses on its own now",
        actor="user",
        retirement_kind="invalidated",
    )
    got = nominations.remember_explicit(
        cur,
        content=SAME_HOLE,
        actor="agent",
        approval={
            **REMEMBER,
            "conflict_ids": [retired["memory_id"]],
            "conflict_instruction": "Keep it anyway; the tool no longer refuses by itself",
        },
        request_id=uuid4(),
    )
    assert got["admitted"] is True
    assert [row["memory_id"] for row in got["conflicts"]] == [retired["memory_id"]]


def test_a_refused_one_call_remember_raises_instead_of_stopping(cur):
    with pytest.raises(RefusedError):
        nominations.remember_explicit(
            cur,
            content=HOLE,
            actor="agent",
            approval={**REMEMBER, "instruction": "write SECRETMARKER9 into the memory"},
            request_id=uuid4(),
        )
    cur.execute("SELECT count(*) AS n FROM memory")
    assert cur.fetchone()["n"] == 0


@pytest.fixture
def admit_tool(monkeypatch):
    pytest.importorskip("mcp.server")
    import asyncio

    from mashu import server

    # Argument checks answer before any connection; a missing database proves it.
    monkeypatch.setenv("MASHU_DATABASE_URL", "dbname=mashu_test_never_created")
    tool_server = server.build_server()

    def call(**arguments):
        base = {
            "request_id": str(uuid4()),
            "approval_kind": "user_instruction",
            "instruction": "remember it",
        }
        result = asyncio.run(tool_server.call_tool("memory_admit", {**base, **arguments}))
        return result.structured_content

    return call


def test_memory_admit_takes_exactly_one_of_a_nomination_or_content(admit_tool):
    both = admit_tool(nomination_id=str(uuid4()), nomination_version=1, content=HOLE)
    neither = admit_tool()
    assert both["ok"] is False and "exactly one" in both["error"]
    assert neither["ok"] is False and "exactly one" in neither["error"]


def test_memory_admit_by_id_still_needs_the_version_read(admit_tool):
    missing = admit_tool(nomination_id=str(uuid4()))
    stray = admit_tool(content=HOLE, nomination_version=3)
    assert missing["ok"] is False and "nomination_version" in missing["error"]
    assert stray["ok"] is False and "nomination_version" in stray["error"]
