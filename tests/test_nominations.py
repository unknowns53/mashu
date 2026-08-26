"""The queue that waits on a person (specification 5.1)."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from mashu import ledger, memories, nominations
from mashu.errors import MashuError, RefusedError

HOLE = "always run the migration before starting the local server"
SAME_HOLE = "always run the migrations before starting the local server"


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
    """What the person approved was this rule standing on these pains.

    A memory that quietly points somewhere else is a different decision from
    the one that was made.
    """
    first, second, nomination = two_pains(cur)
    memory = nominations.admit(cur, nomination["nomination_id"], actor="user", delivery="always")

    assert memory["status"] == "active"
    assert memory["content"] == nomination["content"]
    assert memory["evidence"] == [first["ledger_id"], second["ledger_id"]]

    cur.execute(
        "SELECT status, memory_id FROM nomination WHERE nomination_id = %s",
        (nomination["nomination_id"],),
    )
    row = cur.fetchone()
    assert row["status"] == "admitted"
    assert row["memory_id"] == memory["memory_id"]


def test_the_first_revision_names_where_the_rule_came_from(cur):
    _, _, nomination = two_pains(cur)
    memory = nominations.admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    cur.execute(
        "SELECT content, note FROM memory_revision WHERE memory_id = %s",
        (memory["memory_id"],),
    )
    row = cur.fetchone()
    assert row["content"] == nomination["content"]
    assert str(nomination["nomination_id"]) in row["note"]


def test_an_admission_may_reword_the_rule_and_choose_where_it_lands(cur, scope_id):
    """The evidence proves a hole exists; how to say it is the person's call."""
    _, _, nomination = two_pains(cur, scope_id=scope_id)
    memory = nominations.admit(
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
    assert memory["scope_id"] == scope_id


def test_a_decision_is_made_once(cur):
    _, _, nomination = two_pains(cur)
    nominations.admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    with pytest.raises(MashuError, match="already admitted"):
        nominations.admit(cur, nomination["nomination_id"], actor="user", delivery="always")
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
    nominations.admit(cur, other["nomination_id"], actor="user", delivery="always")
    assert nominations.pending_nominations(cur) == []


def test_putting_a_candidate_off_needs_a_reason_and_does_not_decide_it(cur):
    """The note is all the next reader inherits, because nothing was settled."""
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
    """The decision being asked for is whether these pains justify a seat.

    An identifier answers nothing, and the person deciding cannot go and look
    them up mid-keystroke.
    """
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


# --------------------------------------------------------------------------
# an instruction an agent says it was given (5.1, path three)
# --------------------------------------------------------------------------
def test_a_carried_instruction_reaches_the_queue_and_stops_there(cur, scope_id):
    """What is on file is that an agent said this was asked for.

    That is a different fact from its having been asked for, and the ledger
    row is worded as the first one because the reviewer is the only party who
    can tell them apart.
    """
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
    assert "confirm before it stands" in evidence["what"]
    assert evidence["created_by"] == "the agent"

    cur.execute("SELECT count(*) AS n FROM memory")
    assert cur.fetchone()["n"] == 0


def test_the_same_instruction_carried_twice_does_not_queue_twice(cur):
    first = nominations.nominate_user_explicit(cur, content=HOLE, actor="agent")
    second = nominations.nominate_user_explicit(cur, content=SAME_HOLE, actor="agent")

    assert second["nomination_existing"] is True
    assert second["nomination"]["nomination_id"] == first["nomination"]["nomination_id"]
    assert second["ledger_id"] is None

    cur.execute("SELECT count(*) AS n FROM ledger WHERE kind = 'claimed'")
    assert cur.fetchone()["n"] == 1


def test_a_carried_instruction_cannot_walk_a_retirement_back(cur):
    """Otherwise this is the way round a refutation.

    An agent that read the withdrawn claim somewhere could put it in front of
    a reviewer again with the reason it was withdrawn for left behind.
    """
    _, _, nomination = two_pains(cur)
    memory = nominations.admit(cur, nomination["nomination_id"], actor="user", delivery="always")
    withdrawn = "the tool refuses on its own now"
    memories.retire(cur, memory["memory_id"], reason=withdrawn, actor="user")

    got = nominations.nominate_user_explicit(cur, content=SAME_HOLE, actor="agent")
    assert got["tombstone_suppressed"] is True
    assert got["nomination"] is None
    assert got["ledger_id"] is None
    assert got["matches"]["tombstones"][0]["retire_reason"] == withdrawn


def test_a_banned_pattern_is_refused_before_anything_is_carried(cur):
    with pytest.raises(RefusedError):
        nominations.nominate_user_explicit(
            cur, content="write it under SECRETMARKER3 from now on", actor="agent"
        )
    cur.execute("SELECT count(*) AS n FROM ledger")
    assert cur.fetchone()["n"] == 0


# --------------------------------------------------------------------------
# evidence that names nothing (FIX 7)
# --------------------------------------------------------------------------
def test_evidence_naming_a_row_that_does_not_exist_is_refused_in_words(cur):
    """The array is a plain UUID[], so nothing in the column stops a made-up id."""
    invented = uuid4()
    with pytest.raises(MashuError, match=str(invented)):
        nominations.create_nomination(
            cur, content=HOLE, kind="incident", evidence=[invented], actor="agent"
        )


def test_the_database_refuses_the_same_thing_when_the_code_is_gone_round(cur, scope_id):
    """The Python check is for the message; this is the check that counts.

    A row whose evidence points at nothing looks exactly like a supported one
    until somebody opens it, which is well past the moment the support was
    supposed to exist.
    """
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
