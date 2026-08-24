"""What the running system is costing, and what it cannot yet say (27.3)."""

from __future__ import annotations

import uuid

import psycopg.types.json

from mashu import metrics, proposals, store
from mashu.models import Delivery, MemoryType, ProposalOperation, SourceType, VersionStatus


def _seed(cur, scope_id, *, adopt):
    return store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.PREFERENCE,
        title=f"規律 {uuid.uuid4()}",
        content="短く。",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=adopt,
    )


def test_the_unreviewed_share_is_taken_from_the_store_not_from_a_search(cur, scope_id):
    """27.3 measures the population. A share read off the returned set would be
    measuring whatever cap the retrieval applied, not whether review is keeping
    up with what has accumulated."""
    for _ in range(3):
        _seed(cur, scope_id, adopt=True)
    _seed(cur, scope_id, adopt=False)

    got = metrics.collect(cur)
    row = next(r for r in got.unreviewed if r["held"] == 4)
    assert row["adopted"] == 3
    assert row["unreviewed"] == 1
    assert row["share"] == 0.25


def test_an_indicator_with_no_source_is_named_rather_than_left_out(cur, scope_id):
    """A page showing only the answerable figures reads as a full account of a
    system half of whose failure modes nothing is watching."""
    got = metrics.collect(cur)
    assert got.unmeasured
    assert any("段 D" in line for line in got.unmeasured)
    assert any("not timed" in line for line in got.unmeasured)


def test_the_opening_cost_is_reported_per_scope_against_the_ceiling(cur, scope_id):
    """The fixed cost every session pays before it asks anything (21.2)."""
    memory_id, _ = _seed(cur, scope_id, adopt=True)
    store.set_delivery(cur, memory_id=memory_id, delivery=Delivery.STARTUP_REQUIRED, actor="user")

    got = metrics.collect(cur)
    row = next(r for r in got.openings if r["startup"] >= 1)
    assert row["cost"] <= row["budget"]
    assert row["trimmed"] == 0


def test_a_rejection_counts_as_a_correction(cur, scope_id):
    """27.3 counts what a person had to take back, and sets no threshold on it."""
    before = metrics.collect(cur).corrections["declined"]
    made = proposals.propose(
        cur,
        actor="agent-1",
        operation=ProposalOperation.CREATE,
        payload={
            "scope_id": str(scope_id),
            "type": str(MemoryType.OBSERVATION),
            "title": f"見たこと {uuid.uuid4()}",
            "content": "本文",
            "source_type": str(SourceType.AGENT),
        },
    )
    proposals.reject(cur, made["proposal"]["proposal_id"], reviewer="user", reason="要らない")

    got = metrics.collect(cur)
    assert got.corrections["declined"] == before + 1
    assert got.corrections["per_100_retrievals"] is None or got.corrections["retrievals"] >= 0


def test_a_date_in_the_body_is_provenance_and_is_not_screened(cur, scope_id):
    """25.2's debt is a rule that names its own moment, not one that cites a date.

    Measured against the real store, a body-wide search matched 44 of 94 and
    almost all of them were "on 2026-08-08 the user said" — the ground a rule
    rests on, not the moment it stops being true. Screening those would bury
    the handful that are actually windows.
    """
    store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.PREFERENCE,
        title="外からの連続取得は逐次で行う",
        content="2026-07-07 の遮断を受けた予防措置。**Why:** 2026-08-08 にユーザーが指摘した。",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=True,
    )
    assert not [row for row in metrics.self_dating(cur) if "逐次" in row["title"]]


def test_a_rule_that_names_its_own_moment_is_screened_with_the_match(cur, scope_id):
    """The match is printed, so waving off a wrong one costs a glance."""
    store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.PREFERENCE,
        title="委譲先モデルの選び方——2026-08 時点の顔ぶれ",
        content="本文",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=True,
    )
    row = next(r for r in metrics.self_dating(cur) if "委譲先" in r["title"])
    assert row["matched_in"] == "title"
    assert "2026-08" in row["matched"]


def test_the_short_form_is_screened_too_because_it_is_what_gets_pushed(cur, scope_id):
    """A directive is the rule as every session receives it (21.2)."""
    memory_id, version_id = store.create_entity(
        cur,
        scope_id=scope_id,
        type=MemoryType.PREFERENCE,
        title="枠の使い方",
        content="長い本文",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        adopt=True,
    )
    store.set_directive(
        cur, memory_id=memory_id, directive="当面はこの割り当てで回す。", actor="user"
    )
    row = next(r for r in metrics.self_dating(cur) if r["memory_id"] == memory_id)
    assert row["matched_in"] == "directive"


# --------------------------------------------------------------------------
# 27.4b 改: does the unattended worker find what a person retires
# --------------------------------------------------------------------------
def _ended_session(cur, *, external_id, extracted):
    cur.execute(
        """
        INSERT INTO agent_session (agent, source_cli, external_session_id,
                                   started_at, ended_at)
        VALUES ('claude', 'claude', %s, now() - interval '2 hour',
                now() - interval '1 hour')
        RETURNING session_id
        """,
        (external_id,),
    )
    session_id = cur.fetchone()["session_id"]
    if extracted:
        cur.execute(
            """
            INSERT INTO extraction_run (source_cli, external_session_id,
                                        transcript_digest, extractor_version, state)
            VALUES ('claude', %s, %s, 'v1', 'succeeded')
            """,
            (external_id, str(uuid.uuid4())),
        )
    return session_id


def _marker(cur, scope_id, session_id):
    """A retirement a person stated, timed inside the session's window."""
    memory_id, version_id = _seed(cur, scope_id, adopt=True)
    cur.execute(
        """
        INSERT INTO proposal (actor, operation, target_memory, payload, status,
                              reviewer, decided_at, decision_reason, created_at)
        VALUES ('user', 'change_status', %s, %s, 'approved',
                'user', now() - interval '80 minute', 'stated at the terminal',
                now() - interval '90 minute')
        """,
        (
            memory_id,
            psycopg.types.json.Jsonb(
                {"version_id": str(version_id), "status": "completed", "reason": "終わった"}
            ),
        ),
    )
    return memory_id


def test_a_marker_in_a_log_nobody_read_is_not_counted_as_missed(cur, scope_id):
    """Otherwise "the worker looked and did not find it" is reported for a
    transcript the worker has never opened, and recall reads as failure for
    work that has not happened."""
    session_id = _ended_session(cur, external_id=str(uuid.uuid4()), extracted=False)
    _marker(cur, scope_id, session_id)

    got = metrics.retirement_eval(cur)
    assert got["missed"] == 0
    assert got["unread"] >= 1
    assert got["recall"] is None


def test_a_marker_the_extraction_did_not_recover_is_a_miss(cur, scope_id):
    """With the log actually read, a marker that came back empty is recall."""
    external = str(uuid.uuid4())
    session_id = _ended_session(cur, external_id=external, extracted=True)
    _marker(cur, scope_id, session_id)

    got = metrics.retirement_eval(cur)
    assert got["missed"] >= 1
    assert got["recall"] == 0.0


def test_the_worker_getting_there_first_counts_as_recovered(cur, scope_id):
    """The extraction of the same session proposed the same retirement."""
    external = str(uuid.uuid4())
    session_id = _ended_session(cur, external_id=external, extracted=True)
    memory_id = _marker(cur, scope_id, session_id)
    cur.execute(
        """
        INSERT INTO proposal (actor, operation, target_memory, session_id, payload, status)
        VALUES (%s, 'change_status', %s, %s, %s, 'pending')
        """,
        (
            metrics.WORKER_ACTOR,
            memory_id,
            session_id,
            psycopg.types.json.Jsonb({"status": "completed", "reason": "見つけた"}),
        ),
    )

    got = metrics.retirement_eval(cur)
    assert got["recovered"] >= 1
    assert got["recall"] == 1.0


def _standing(cur, scope_id, *, type, title, content="本文"):
    memory_id, _ = store.create_entity(
        cur,
        scope_id=scope_id,
        type=type,
        title=title,
        content=content,
        source_type=SourceType.AGENT,
        created_by="mashu-worker",
        actor="mashu-worker",
        adopt=True,
    )
    return memory_id


def test_a_standing_task_is_swept_whatever_it_says(cur, scope_id):
    """13.1 puts only the indefinite kind in Memory; a task from a transcript is not."""
    memory_id = _standing(cur, scope_id, type=MemoryType.TASK, title="測定器の配線欠陥を直す")
    found = {row["memory_id"]: row for row in metrics.rot_prone(cur)}
    assert memory_id in found
    assert "a task stands until it is finished" in found[memory_id]["why"]


def test_a_standing_state_is_swept_because_the_next_one_ends_it(cur, scope_id):
    memory_id = _standing(cur, scope_id, type=MemoryType.STATE, title="いまの現在地")
    found = {row["memory_id"]: row for row in metrics.rot_prone(cur)}
    assert memory_id in found
    assert "only until the next one" in found[memory_id]["why"]


def test_a_task_whose_title_says_it_is_over_is_read_as_that(cur, scope_id):
    """A completion recorded as something still to do is the shape worth naming."""
    memory_id = _standing(cur, scope_id, type=MemoryType.TASK, title="Phase 3 を完了した")
    found = {row["memory_id"]: row for row in metrics.rot_prone(cur)}
    assert "the title says this is finished" in found[memory_id]["why"]


def test_an_ordinary_standing_rule_is_left_off_the_sweep(cur, scope_id):
    """A list that flags everything standing is a list of everything standing."""
    memory_id = _standing(
        cur,
        scope_id,
        type=MemoryType.PREFERENCE,
        title="測ってから値を置く",
        content="推測で置いた値は下流へ渡さない。",
    )
    assert memory_id not in [row["memory_id"] for row in metrics.rot_prone(cur)]


def test_what_is_no_longer_standing_is_not_swept_again(cur, scope_id):
    """Retiring is what takes something off this list, through the path a person uses."""
    memory_id = _standing(cur, scope_id, type=MemoryType.TASK, title="もう終わった作業")
    entity = store.get_entity(cur, memory_id)
    # a completion the user states is auto committed by the gate (17), which is
    # the path 'mashu retire' takes and so the one this has to be measured on
    proposals.propose(
        cur,
        actor="user",
        operation=ProposalOperation.CHANGE_STATUS,
        payload={
            "version_id": str(entity["active_version"]),
            "status": str(VersionStatus.COMPLETED),
            "reason": "終わったため",
            "source_type": str(SourceType.USER),
        },
        target_memory=memory_id,
    )

    assert memory_id not in [row["memory_id"] for row in metrics.rot_prone(cur)]


def test_something_read_and_left_standing_stops_coming_back_for_a_while(cur, scope_id):
    """Without this the only key that dismisses anything is a retirement.

    A screen whose only way to say "not this one" is to retire it collects
    retirements that were meant as dismissals, and a later session is handed
    those as readings of the knowledge.
    """
    from datetime import datetime, timedelta

    memory_id = _standing(cur, scope_id, type=MemoryType.TASK, title="まだ続いている作業")
    assert memory_id in [row["memory_id"] for row in metrics.rot_prone(cur)]

    store.confirm_standing(
        cur,
        memory_id=memory_id,
        version_id=store.get_entity(cur, memory_id)["active_version"],
        until=datetime.now().astimezone() + timedelta(days=30),
        actor="user",
    )
    assert memory_id not in [row["memory_id"] for row in metrics.rot_prone(cur)]


def test_a_confirmation_that_has_run_out_puts_it_back(cur, scope_id):
    from datetime import datetime, timedelta

    memory_id = _standing(cur, scope_id, type=MemoryType.TASK, title="また見る時期の来た作業")
    store.confirm_standing(
        cur,
        memory_id=memory_id,
        version_id=store.get_entity(cur, memory_id)["active_version"],
        until=datetime.now().astimezone() - timedelta(days=1),
        actor="user",
    )
    assert memory_id in [row["memory_id"] for row in metrics.rot_prone(cur)]


def test_rewriting_the_memory_ends_the_confirmation(cur, scope_id):
    """What was confirmed is the text that was read, not the entity for ever."""
    from datetime import datetime, timedelta

    memory_id = _standing(cur, scope_id, type=MemoryType.TASK, title="書き直される作業")
    store.confirm_standing(
        cur,
        memory_id=memory_id,
        version_id=store.get_entity(cur, memory_id)["active_version"],
        until=datetime.now().astimezone() + timedelta(days=30),
        actor="user",
    )
    assert memory_id not in [row["memory_id"] for row in metrics.rot_prone(cur)]

    store.add_version(
        cur,
        memory_id=memory_id,
        content="事情が変わって、別のことをする作業になった。",
        source_type=SourceType.USER,
        created_by="user",
        actor="user",
        based_on_version=store.get_entity(cur, memory_id)["latest_version"],
        adopt=True,
    )

    assert memory_id in [row["memory_id"] for row in metrics.rot_prone(cur)]
