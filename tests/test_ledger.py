
from __future__ import annotations

import pytest

from mashu import ledger, memories, nominations
from mashu.errors import MashuError, RefusedError

# Trigram fixtures use near-identical and unrelated text.
HOLE = "always run the migration before starting the local server"
SAME_HOLE = "always run the migrations before starting the local server"
OTHER_HOLE = "quotas on the shared queue reset at midnight every day"


def report(cur, kind, prevention, what="work went down a wrong path", **kw):
    return ledger.report_pain(cur, kind=kind, what=what, prevention=prevention, actor="agent", **kw)


def test_an_incident_is_proven_the_first_time(cur):
    got = report(cur, "incident", HOLE)
    nomination = got["nomination"]
    assert nomination["kind"] == "incident"
    assert nomination["content"] == HOLE
    assert nomination["evidence"] == [got["ledger_id"]]
    assert got["nomination_existing"] is False


def test_the_first_friction_only_lands_in_the_ledger(cur):
    got = report(cur, "friction", HOLE)
    assert got["nomination"] is None
    assert ledger.ledger_entries(cur)[0]["kind"] == "friction"


def test_the_second_friction_cites_both_times(cur):
    first = report(cur, "friction", HOLE)
    second = report(cur, "friction", SAME_HOLE)

    nomination = second["nomination"]
    assert nomination["kind"] == "rederivation"
    assert nomination["evidence"] == [first["ledger_id"], second["ledger_id"]]


def test_a_statement_is_not_the_other_half_of_a_rederivation(cur):
    memories.remember(cur, content=HOLE, actor="user")
    got = report(cur, "friction", SAME_HOLE)

    assert got["nomination"] is None
    assert got["tombstone_suppressed"] is False
    # The statement is still shown; only the choice of prior is narrowed.
    assert [row["kind"] for row in got["matches"]["ledger"]] == ["explicit"]

    # The same holds for a claimed row.
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
    with pytest.raises(MashuError, match="reserved"):
        report(cur, "explicit", HOLE)


def test_a_banned_pattern_is_refused_and_nothing_is_written(cur):
    with pytest.raises(RefusedError):
        report(cur, "incident", "the path under SECRETMARKER1 is the one that matters")

    cur.execute("SELECT count(*) AS n FROM ledger")
    assert cur.fetchone()["n"] == 0
    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 0


def test_a_pain_landing_on_retired_knowledge_does_not_nominate(cur):
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


def test_a_pain_landing_on_a_rule_already_delivered_indicts_the_delivery(cur):
    kept = memories.remember(cur, content=HOLE, actor="user")

    got = report(cur, "incident", SAME_HOLE)

    assert got["delivery_suspect"] is True
    assert got["nomination"] is None
    assert got["matches"]["memories"][0]["memory_id"] == kept["memory_id"]
    assert "delivery" in got["note"]

    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 0
    cur.execute(
        "SELECT memory_id, ledger_id FROM event_log WHERE event_type = 'delivery_failure_suspected'"
    )
    seen = cur.fetchall()
    assert [row["memory_id"] for row in seen] == [kept["memory_id"]]
    assert [row["ledger_id"] for row in seen] == [got["ledger_id"]]


def test_a_retirement_is_read_before_an_active_rule_is(cur):
    kept = memories.remember(cur, content=HOLE, actor="user")
    memories.retire(cur, kept["memory_id"], reason="the step moved into the server", actor="user")

    got = report(cur, "friction", SAME_HOLE)

    assert got["tombstone_suppressed"] is True
    assert got["delivery_suspect"] is False


def test_a_third_pain_lands_under_the_candidate_instead_of_beside_it(cur):
    first = report(cur, "friction", HOLE)
    second = report(cur, "friction", SAME_HOLE)
    third = report(cur, "friction", HOLE)

    assert third["nomination_existing"] is True
    nomination = third["nomination"]
    assert nomination["nomination_id"] == second["nomination"]["nomination_id"]
    assert nomination["evidence"] == [
        first["ledger_id"],
        second["ledger_id"],
        third["ledger_id"],
    ]

    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 1


def test_a_candidate_put_off_comes_back_when_the_hole_reopens(cur):
    report(cur, "friction", HOLE)
    second = report(cur, "friction", SAME_HOLE)
    nomination_id = second["nomination"]["nomination_id"]
    nominations.defer(cur, nomination_id, actor="user", reason="wording is not settled")
    assert nominations.pending_nominations(cur, include_deferred=False) == []

    report(cur, "friction", HOLE)

    back = nominations.pending_nominations(cur, include_deferred=False)
    assert [row["nomination_id"] for row in back] == [nomination_id]
    assert back[0]["deferred_at"] is None
    assert back[0]["defer_reason"] == "wording is not settled"


# a prevention that is work, not a rule (v3 9)

FIX = "have session_bootstrap name the migrations that have not been applied"


@pytest.fixture
def work_task(cur):
    from mashu import projects, tasks

    project = projects.create_project(cur, name="mashu", actor="user")
    return tasks.task_create(
        cur,
        project=project["project_id"],
        name="put v3 into service",
        next_actions=["apply the migrations"],
        actor="agent",
    )


def test_work_never_asks_for_a_seat(cur, work_task):
    got = report(cur, "incident", FIX, prevention_kind="work", task_id=work_task["task"]["task_id"])

    assert got["nomination"] is None
    assert got["prevention_kind"] == "work"
    assert nominations.pending_nominations(cur) == []


def test_work_lands_on_the_task_that_will_make_it(cur, work_task):
    from mashu import tasks

    task_id = work_task["task"]["task_id"]
    got = report(cur, "incident", FIX, prevention_kind="work", task_id=task_id)

    assert got["filed_task"] == task_id
    assert tasks.task_get(cur, task_id)["state"]["next_actions"] == [
        "apply the migrations",
        FIX,
    ]
    assert ledger.ledger_entries(cur)[0]["filed_task"] == task_id


def test_work_with_no_task_says_so_instead_of_going_quiet(cur):
    got = report(cur, "incident", FIX, prevention_kind="work")

    assert got["nomination"] is None
    assert got["filed_task"] is None
    assert "filed nowhere" in got["note"]
    assert ledger.ledger_entries(cur)[0]["prevention"] == FIX


def test_a_refused_filing_does_not_take_the_pain_down_with_it(cur, work_task):
    from mashu import tasks

    task_id = work_task["task"]["task_id"]
    tasks.close(cur, task_id, outcome="completed", actor="user")

    got = report(cur, "incident", FIX, prevention_kind="work", task_id=task_id)

    assert got["filed_task"] is None
    assert "still needs a home" in got["note"]
    assert ledger.ledger_entries(cur)[0]["prevention"] == FIX


def test_a_rule_is_not_filed_on_a_task(cur, work_task):
    with pytest.raises(MashuError):
        report(cur, "incident", FIX, task_id=work_task["task"]["task_id"])


def test_an_unknown_prevention_kind_is_refused(cur):
    with pytest.raises(MashuError):
        report(cur, "incident", FIX, prevention_kind="maybe")


def test_a_work_friction_can_still_be_the_prior_half_of_a_rederivation(cur, work_task):
    first = report(
        cur, "friction", HOLE, prevention_kind="work", task_id=work_task["task"]["task_id"]
    )
    assert first["nomination"] is None

    second = report(cur, "friction", SAME_HOLE)

    assert second["nomination"]["kind"] == "rederivation"
    assert second["nomination"]["evidence"] == [first["ledger_id"], second["ledger_id"]]


def test_a_full_task_refuses_the_filing_and_keeps_the_pain(cur, work_task):
    from mashu import tasks

    task_id = work_task["task"]["task_id"]
    at = tasks.task_get(cur, task_id)["state"]["updated_at"]
    tasks.task_update(
        cur,
        task_id,
        actor="agent",
        expect_updated_at=at,
        next_actions=[f"action {n}" for n in range(tasks.LIST_MAX_ITEMS)],
    )

    got = report(cur, "incident", FIX, prevention_kind="work", task_id=task_id)

    assert got["filed_task"] is None
    assert "still needs a home" in got["note"]
    assert ledger.ledger_entries(cur)[0]["prevention"] == FIX


def test_a_rule_row_can_never_carry_a_filing(cur, work_task):
    import psycopg.errors

    with pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO ledger (kind, what, prevention, created_by,
                                prevention_kind, filed_task)
            VALUES ('incident', 'w', 'p', 'agent', 'rule', %s)
            """,
            (work_task["task"]["task_id"],),
        )
