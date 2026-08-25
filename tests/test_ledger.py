"""The pain ledger and what it nominates (specification 3, 4.1)."""

from __future__ import annotations

import pytest

from mashu import ledger, memories, nominations
from mashu.errors import MashuError, RefusedError

# Near-identical text is how the suite says "the same hole"; text with no
# shared substrings is how it says "a different one". Both are ASCII so that
# trigram similarity stays the same number on every run.
HOLE = "always run the migration before starting the local server"
SAME_HOLE = "always run the migrations before starting the local server"
OTHER_HOLE = "quotas on the shared queue reset at midnight every day"


def report(cur, kind, prevention, what="work went down a wrong path", **kw):
    return ledger.report_pain(cur, kind=kind, what=what, prevention=prevention, actor="agent", **kw)


def test_an_incident_is_proven_the_first_time(cur):
    """Section 3: one wrong result is evidence.

    Waiting for a second occurrence of something that already went wrong is a
    policy of paying for the same hole twice on purpose.
    """
    got = report(cur, "incident", HOLE)
    nomination = got["nomination"]
    assert nomination["kind"] == "incident"
    assert nomination["content"] == HOLE
    assert nomination["evidence"] == [got["ledger_id"]]
    assert got["nomination_existing"] is False


def test_the_first_friction_only_lands_in_the_ledger(cur):
    """The first time something is looked up it is work, not a hole."""
    got = report(cur, "friction", HOLE)
    assert got["nomination"] is None
    assert ledger.ledger_entries(cur)[0]["kind"] == "friction"


def test_the_second_friction_cites_both_times(cur):
    """This is the whole reason the first one was written down."""
    first = report(cur, "friction", HOLE)
    second = report(cur, "friction", SAME_HOLE)

    nomination = second["nomination"]
    assert nomination["kind"] == "rederivation"
    assert nomination["evidence"] == [first["ledger_id"], second["ledger_id"]]


def test_a_statement_is_not_the_other_half_of_a_rederivation(cur):
    """Only a friction or a trace can be the prior (4.1).

    An explicit or claimed row is somebody stating a rule, not anybody having
    worked something out twice. Counting them would let a single look-up next
    to an existing statement call itself a second derivation, which is the
    standard in section 3 being satisfied by paraphrase. The rule matters most
    where the statement is alive: the memory here is active, so there is no
    tombstone doing the refusing.
    """
    memories.remember(cur, content=HOLE, actor="user")
    got = report(cur, "friction", SAME_HOLE)

    assert got["nomination"] is None
    assert got["tombstone_suppressed"] is False
    # The statement is still shown; only the choice of prior is narrowed.
    assert [row["kind"] for row in got["matches"]["ledger"]] == ["explicit"]

    # The same holds for a claimed row. Its own candidate is taken out of the
    # queue first, so what is left to match against is the statement alone.
    claimed = nominations.nominate_user_explicit(cur, content=OTHER_HOLE, actor="agent")
    nominations.decline(
        cur, claimed["nomination"]["nomination_id"], actor="user", reason="said in passing"
    )
    again = report(cur, "friction", OTHER_HOLE)
    assert again["nomination"] is None
    assert [row["kind"] for row in again["matches"]["ledger"]] == ["claimed"]


def test_a_different_friction_is_still_a_first_time(cur):
    report(cur, "friction", HOLE)
    got = report(cur, "friction", OTHER_HOLE)
    assert got["nomination"] is None


def test_a_candidate_already_waiting_is_not_filed_twice(cur):
    """Two rows saying one thing cost two decisions and admit one rule.

    The duplicate is invisible until somebody reads both, which is after the
    cost has been paid.
    """
    report(cur, "friction", HOLE)
    second = report(cur, "friction", SAME_HOLE)
    third = report(cur, "friction", SAME_HOLE)

    assert third["nomination_existing"] is True
    assert third["nomination"]["nomination_id"] == second["nomination"]["nomination_id"]

    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 1


def test_the_matches_come_back_whether_or_not_anything_is_nominated(cur):
    report(cur, "friction", HOLE)
    got = report(cur, "friction", SAME_HOLE)
    assert [row["prevention"] for row in got["matches"]["ledger"]] == [HOLE]
    assert got["matches"]["traces"] == []
    assert got["matches"]["tombstones"] == []


def test_a_person_recording_by_hand_does_not_come_through_here(cur):
    """'explicit' is the one kind this path may not write.

    The immediate route to active is a person at their own terminal, and an
    agent able to write that kind here would be able to spell one.
    """
    with pytest.raises(MashuError, match="reserved"):
        report(cur, "explicit", HOLE)


def test_a_banned_pattern_is_refused_and_nothing_is_written(cur):
    """The gate is at the entrance, so a refusal leaves no half-written row."""
    with pytest.raises(RefusedError):
        report(cur, "incident", "the path under SECRETMARKER1 is the one that matters")

    cur.execute("SELECT count(*) AS n FROM ledger")
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 0


def test_a_pain_landing_on_retired_knowledge_does_not_nominate(cur):
    """Refuted content does not come back through the automatic door.

    The nomination would put the retired sentence in front of a reviewer who
    is never shown the refutation. What the reporter gets is the retire
    reason; overriding it stays a human act.
    """
    from mashu import memories

    kept = memories.remember(cur, content=HOLE, actor="user")
    memories.retire(
        cur, kept["memory_id"], reason="the migration step moved into the server", actor="user"
    )

    for kind in ("friction", "incident"):
        got = report(cur, kind, SAME_HOLE)
        assert got["tombstone_suppressed"] is True
        assert got["nomination"] is None
        assert got["matches"]["tombstones"][0]["retire_reason"]

    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 0


def test_the_ledger_lists_the_scope_it_was_filtered_by(cur, scope_id):
    report(cur, "friction", HOLE, scope_id=scope_id)
    report(cur, "friction", OTHER_HOLE)
    assert len(ledger.ledger_entries(cur)) == 2
    scoped = ledger.ledger_entries(cur, scope_id=scope_id)
    assert [row["prevention"] for row in scoped] == [HOLE]
