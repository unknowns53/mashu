"""The queue that waits on a person (specification 5.1)."""

from __future__ import annotations

import pytest

from mashu import ledger, nominations
from mashu.errors import MashuError

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
