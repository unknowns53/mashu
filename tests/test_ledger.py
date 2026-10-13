from __future__ import annotations

from uuid import uuid4

import pytest

from conftest import new_project, new_task, remember, retire, update_task
from mashu import application, ledger, memories, nominations, tasks, topics
from mashu.errors import MalformedRequestError, MashuError, RefusedError

# Trigram fixtures use near-identical and unrelated text.
HOLE = "always run the migration before starting the local server"
SAME_HOLE = "always run the migrations before starting the local server"
OTHER_HOLE = "quotas on the shared queue reset at midnight every day"


def report(cur, kind, prevention, what="work went down a wrong path", **kw):
    return ledger.report_pain(cur, kind=kind, what=what, prevention=prevention, actor="agent", **kw)


def count(cur, table):
    cur.execute(f"SELECT count(*) AS n FROM {table}")
    return cur.fetchone()["n"]


def test_a_friction_is_refused_and_nothing_is_written(cur):
    with pytest.raises(MashuError, match="'friction' is no longer reported"):
        report(cur, "friction", HOLE)

    assert count(cur, "ledger") == 0
    assert count(cur, "nomination") == 0


def test_an_incident_nominates_at_once_and_shows_the_pains_it_resembles(cur):
    first = report(cur, "incident", HOLE)
    assert first["nomination"]["kind"] == "incident"
    assert first["nomination"]["evidence"] == [first["ledger_id"]]
    assert set(first["matches"]) == {"ledger", "tombstones", "memories"}
    assert first["violated_memory_id"] is None


def test_a_later_pain_lands_under_the_candidate_instead_of_beside_it(cur):
    first = report(cur, "incident", HOLE)
    second = report(cur, "incident", SAME_HOLE)
    third = report(cur, "incident", HOLE)

    assert (second["nomination_existing"], third["nomination_existing"]) == (True, True)
    nomination = third["nomination"]
    assert nomination["nomination_id"] == first["nomination"]["nomination_id"]
    assert nomination["evidence"] == [first["ledger_id"], second["ledger_id"], third["ledger_id"]]
    assert count(cur, "nomination") == 1


def test_hand_recording_and_a_banned_pattern_are_refused_and_nothing_is_written(cur):
    with pytest.raises(MashuError, match="reserved"):
        report(cur, "explicit", HOLE)
    with pytest.raises(RefusedError):
        report(cur, "incident", "the path under SECRETMARKER1 is the one that matters")

    assert count(cur, "ledger") == 0
    assert count(cur, "nomination") == 0


@pytest.mark.parametrize("retirement_kind", ("invalidated", "legacy"))
def test_a_new_incident_on_invalidated_or_legacy_retirement_keeps_a_conflicted_candidate(
    cur, retirement_kind
):
    kept = remember(cur, HOLE)
    retire(cur, kept, "the migration step moved into the server", retirement_kind)

    incident = report(cur, "incident", SAME_HOLE)
    assert incident["tombstone_suppressed"] is False
    assert incident["delivery_suspect"] is False
    assert incident["matches"]["tombstones"][0]["retire_reason"]
    conflict = incident["retirement_conflicts"][0]
    assert conflict["retire_reason"] == "the migration step moved into the server"
    assert incident["nomination"] is not None
    assert incident["nomination"]["conflicts"] == [kept["memory_id"]]
    assert incident["retirement_conflicts"][0]["retirement_kind"] == retirement_kind
    assert count(cur, "nomination") == 1


@pytest.mark.parametrize("retirement_kind", ("out_of_scope", "relocated"))
def test_out_of_scope_and_relocated_memories_do_not_suppress_new_candidates(cur, retirement_kind):
    kept = remember(cur, HOLE)
    if retirement_kind == "relocated":
        converted = memories.convert_to_temporary(cur, kept["memory_id"], days=1, actor="user")
        destination = converted["temporary"]
    else:
        retire(cur, kept, "the project left the deployment scope", retirement_kind)

    got = report(cur, "incident", SAME_HOLE)
    assert got["tombstone_suppressed"] is False
    assert got["nomination"] is not None
    assert got["nomination"]["conflicts"] == [kept["memory_id"]]
    conflict = got["retirement_conflicts"][0]
    assert conflict["retirement_kind"] == retirement_kind
    if retirement_kind == "relocated":
        assert conflict["relocated_to_id"] == destination["context_id"]


def test_the_ledger_lists_the_scope_it_was_filtered_by(cur, scope_id):
    report(cur, "incident", HOLE, scope_id=scope_id)
    report(cur, "incident", OTHER_HOLE)
    assert len(ledger.ledger_entries(cur)) == 2
    scoped = ledger.ledger_entries(cur, scope_id=scope_id)
    assert [row["prevention"] for row in scoped] == [HOLE]


def test_a_pain_landing_on_a_rule_already_delivered_indicts_the_delivery(cur):
    old = remember(cur, HOLE)
    kept = remember(cur, HOLE)
    retire(cur, old, "replaced", "superseded", superseded_by=kept["memory_id"])

    got = report(cur, "incident", SAME_HOLE)

    assert got["delivery_suspect"] is True
    assert got["nomination"] is None
    assert got["retirement_conflicts"][0]["superseded_by"] == kept["memory_id"]
    assert got["matches"]["memories"][0]["memory_id"] == kept["memory_id"]
    assert "delivery" in got["note"]
    assert count(cur, "nomination") == 0
    cur.execute(
        "SELECT memory_id, ledger_id FROM event_log WHERE event_type = 'delivery_failure_suspected'"
    )
    seen = cur.fetchall()
    assert [row["memory_id"] for row in seen] == [kept["memory_id"]]
    assert [row["ledger_id"] for row in seen] == [got["ledger_id"]]


def test_a_candidate_put_off_comes_back_when_the_hole_reopens(cur):
    nomination_id = report(cur, "incident", HOLE)["nomination"]["nomination_id"]
    nominations.defer(cur, nomination_id, actor="user", reason="wording is not settled")
    assert nominations.pending_nominations(cur, include_deferred=False) == []

    report(cur, "incident", SAME_HOLE)

    back = nominations.pending_nominations(cur, include_deferred=False)
    assert [row["nomination_id"] for row in back] == [nomination_id]
    assert back[0]["deferred_at"] is None
    assert back[0]["defer_reason"] == "wording is not settled"


# a prevention that is work, not a rule (v3 9)

FIX = "have session_bootstrap name the migrations that have not been applied"


@pytest.fixture
def task_id(cur):
    new_project(cur, "mashu")
    made = new_task(cur, "put v3 into service", "mashu", next_actions=["apply the migrations"])
    return made["task"]["task_id"]


def test_work_never_asks_for_a_seat_and_lands_on_the_task_that_will_make_it(cur, task_id):
    got = report(cur, "incident", FIX, prevention_kind="work", task_id=task_id)

    assert got["nomination"] is None
    assert got["prevention_kind"] == "work"
    assert nominations.pending_nominations(cur) == []
    assert got["filed_task"] == task_id
    next_actions = tasks.task_get(cur, task_id)["state"]["next_actions"]
    assert next_actions == ["apply the migrations", FIX]
    assert ledger.ledger_entries(cur)[0]["filed_task"] == task_id


def test_a_rule_prevention_over_the_body_ceiling_is_refused_and_work_is_not(cur, task_id):
    long_fix = FIX + ", each with the date it was written and the session that wrote it"
    assert len(long_fix) > memories.CONTENT_LIMIT

    with pytest.raises(MalformedRequestError, match="prevention is .* put .* in what"):
        report(cur, "incident", long_fix)
    assert count(cur, "ledger") == 0

    got = report(cur, "incident", long_fix, prevention_kind="work", task_id=task_id)
    assert got["filed_task"] == task_id


@pytest.mark.parametrize(
    ("home", "note"),
    [("none", "filed nowhere"), ("closed", "still needs a home"), ("full", "still needs a home")],
)
def test_work_that_finds_no_home_says_so_and_keeps_the_pain(cur, task_id, home, note):
    if home == "closed":
        tasks.close(cur, task_id, outcome="completed", actor="user")
    elif home == "full":
        update_task(cur, task_id, next_actions=[f"n {n}" for n in range(tasks.LIST_MAX_ITEMS)])

    got = report(
        cur, "incident", FIX, prevention_kind="work", task_id=None if home == "none" else task_id
    )

    assert got["nomination"] is None
    assert got["filed_task"] is None
    assert note in got["note"]
    assert ledger.ledger_entries(cur)[0]["prevention"] == FIX


def test_a_rule_is_never_filed_on_a_task_and_an_unknown_prevention_kind_is_refused(cur, task_id):
    with pytest.raises(MashuError):
        report(cur, "incident", FIX, task_id=task_id)
    with pytest.raises(MashuError):
        report(cur, "incident", FIX, prevention_kind="maybe")


@pytest.mark.parametrize(
    ("read_first", "session", "action", "topic_read", "route"),
    [
        (True, uuid4(), "delegate", True, "topic_read"),
        (False, uuid4(), None, False, "topic_unread"),
        (False, uuid4(), "delegate", False, "guard"),
        (False, None, None, None, "unknown"),
    ],
)
def test_a_pain_on_a_topic_rule_says_whether_its_session_read_the_topic(
    cur, read_first, session, action, topic_read, route
):
    rule = "keep ship stats fixed when calibrating difficulty levels"
    topic = topics.create_topic(cur, name="calibration", trigger="Before calibrating", actor="user")
    topics.update_topic(cur, topic["topic_id"], actor="user", action=action)
    remember(cur, rule, delivery="topic", topic_id=topic["topic_id"])
    if read_first:
        topics.read_for_session(cur, "calibration", actor="agent", session=session)

    report(cur, "incident", rule, what="changed ship stats", session=session)

    cur.execute("SELECT detail FROM event_log WHERE event_type = 'delivery_failure_suspected'")
    detail = cur.fetchone()["detail"]
    assert detail["delivery"] == "topic"
    assert (detail["topic_read"], detail["topic_action"]) == (topic_read, action)
    assert application.status_snapshot(cur).delivery_failures_by_route == {route: 1}


# a rule the reporter names as delivered and not followed (4.1)

#: Worded so that similarity cannot connect it to the rule it broke.
PARAPHRASE = "the server came up against tables that were never created"


def test_a_named_rule_not_followed_is_stored_and_counted_as_a_delivery_failure(cur):
    kept = remember(cur, HOLE)

    got = report(cur, "incident", PARAPHRASE, violated_memory_id=kept["memory_id"])

    assert got["delivery_suspect"] is True
    assert got["nomination"] is None
    assert got["violated_memory_id"] == kept["memory_id"]
    assert "already active" in got["note"]
    cur.execute("SELECT violated_memory_id FROM ledger WHERE ledger_id = %s", (got["ledger_id"],))
    assert cur.fetchone()["violated_memory_id"] == kept["memory_id"]
    assert count(cur, "nomination") == 0
    cur.execute(
        "SELECT memory_id, ledger_id, detail FROM event_log "
        "WHERE event_type = 'delivery_failure_suspected'"
    )
    (seen,) = cur.fetchall()
    assert (seen["memory_id"], seen["ledger_id"]) == (kept["memory_id"], got["ledger_id"])
    assert seen["detail"] == {"delivery": "always", "found_by": "reported"}
    assert application.status_snapshot(cur).delivery_failures_by_route == {"pushed": 1}


def test_a_named_rule_on_a_work_prevention_is_counted_and_the_work_still_filed(cur, task_id):
    kept = remember(cur, HOLE)

    got = report(
        cur,
        "incident",
        FIX,
        prevention_kind="work",
        task_id=task_id,
        violated_memory_id=kept["memory_id"],
    )

    assert got["filed_task"] == task_id
    assert got["delivery_suspect"] is True
    assert count(cur, "event_log WHERE event_type = 'delivery_failure_suspected'") == 1


def test_a_retired_or_unknown_rule_cannot_be_named_as_not_followed(cur):
    kept = remember(cur, HOLE)
    retire(cur, kept, "the server migrates on start")

    with pytest.raises(MashuError, match="retired .* cannot have been the rule not followed"):
        report(cur, "incident", PARAPHRASE, violated_memory_id=kept["memory_id"])
    with pytest.raises(MashuError, match="no memory"):
        report(cur, "incident", PARAPHRASE, violated_memory_id=uuid4())

    assert count(cur, "ledger WHERE kind = 'incident'") == 0
    assert count(cur, "nomination") == 0


def test_a_similar_rule_is_still_found_and_says_how(cur):
    kept = remember(cur, HOLE)

    report(cur, "incident", SAME_HOLE)

    cur.execute(
        "SELECT memory_id, detail FROM event_log WHERE event_type = 'delivery_failure_suspected'"
    )
    seen = cur.fetchone()
    assert seen["memory_id"] == kept["memory_id"]
    assert seen["detail"]["found_by"] == "similarity" and seen["detail"]["score"] >= 0.45


def test_the_server_offers_no_trace_tools_and_pain_report_takes_the_rule_not_followed(mcp, dsn):
    import asyncio

    from mashu import db, server

    names = {tool.name for tool in asyncio.run(server.build_server().list_tools())}
    assert "pain_report" in names
    assert not names & {"trace_put", "trace_search"}
    assert "trace_put" not in server.INSTRUCTIONS and "friction" not in server.INSTRUCTIONS

    refused = mcp("pain_report", kind="friction", what="looked it up again", prevention=HOLE)
    assert refused["ok"] is False and "'friction' is no longer reported" in refused["error"]

    with db.transaction(dsn) as cur:
        kept = remember(cur, HOLE)
    got = mcp(
        "pain_report",
        what="started the server and it failed on missing tables",
        prevention=PARAPHRASE,
        violated_memory_id=str(kept["memory_id"]),
    )
    assert got["ok"] is True and got["delivery_suspect"] is True
    assert got["violated_memory_id"] == str(kept["memory_id"])
