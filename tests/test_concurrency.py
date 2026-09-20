from __future__ import annotations

from collections.abc import Iterator
from threading import Event, Thread
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from mashu import ledger, memories, memory_changes, nominations, scopes
from mashu.errors import MashuError, RetiredConflictError

RULE = "always run the migration before starting the local server"
OTHER_RULE = "answer in the language the question was asked in"
HOLE = "the deployment order is written down in the runbook, not guessed"
SAME_HOLE = "the deployment order is written down in the runbook, never guessed"


@pytest.fixture
def two(committing_dsn: str) -> Iterator[tuple[psycopg.Connection, psycopg.Connection]]:
    """Two connections to the committing database, both rolled back afterwards."""
    first = psycopg.connect(committing_dsn, row_factory=dict_row)
    second = psycopg.connect(committing_dsn, row_factory=dict_row)
    try:
        yield first, second
    finally:
        for conn in (first, second):
            conn.rollback()
            conn.close()


def impatient(conn: psycopg.Connection) -> psycopg.Cursor:
    """A cursor that refuses to wait, so a held lock shows up as a failure."""
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '500ms'")
    return cur


def test_two_admissions_cannot_read_the_same_free_seat(two):
    first, second = two
    with first.cursor() as writing:
        memories.remember(writing, content=RULE, actor="user")

        with impatient(second) as waiting, pytest.raises(psycopg.errors.LockNotAvailable):
            memories.remember(waiting, content=OTHER_RULE, actor="user")


def test_direct_remember_serializes_with_retirement_conflict_checks(two):
    first, second = two
    content = f"the race-only migration check is {uuid4()}"
    with first.cursor() as writing:
        memory = memories.remember(writing, content=content, actor="user")
        memories.retire(
            writing,
            memory["memory_id"],
            reason="the migration now runs before startup",
            retirement_kind="invalidated",
            actor="user",
        )

        with impatient(second) as waiting, pytest.raises(psycopg.errors.LockNotAvailable):
            memories.remember(waiting, content=content, actor="user")

    first.commit()
    second.rollback()
    with second.cursor() as writing, pytest.raises(RetiredConflictError):
        memories.remember(writing, content=content, actor="user")
    second.rollback()


def test_two_pains_cannot_be_matched_against_each_other_at_once(two):
    first, second = two
    with first.cursor() as writing:
        ledger.report_pain(
            writing,
            kind="incident",
            what="deployed the wrong branch",
            prevention=HOLE,
            actor="agent",
        )

        with impatient(second) as waiting, pytest.raises(psycopg.errors.LockNotAvailable):
            ledger.report_pain(
                waiting,
                kind="incident",
                what="deployed the wrong branch again",
                prevention=SAME_HOLE,
                actor="agent",
            )


def test_a_nomination_being_decided_is_held_against_the_other_decision(committing_dsn, two):
    with psycopg.connect(committing_dsn, row_factory=dict_row) as setup, setup.cursor() as cur:
        scope = scopes.create_scope(cur, name="a scope for the race", actor="user")
        nomination = nominations.nominate_user_explicit(
            cur, content=RULE, actor="agent", scope_id=scope["scope_id"]
        )["nomination"]

    first, second = two
    with first.cursor() as deciding:
        nominations.admit(
            deciding,
            nomination["nomination_id"],
            actor="user",
            delivery="always",
            expected_version=nomination["version"],
            approval={"kind": "user_direct"},
            request_id=uuid4(),
        )

        with impatient(second) as waiting, pytest.raises(psycopg.errors.LockNotAvailable):
            nominations.decline(
                waiting, nomination["nomination_id"], actor="user", reason="not worth a seat"
            )


def test_a_candidate_decided_once_is_not_decided_again(committing_dsn):
    with psycopg.connect(committing_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        nomination = nominations.nominate_user_explicit(cur, content=OTHER_RULE, actor="agent")[
            "nomination"
        ]
        nominations.admit(
            cur,
            nomination["nomination_id"],
            actor="user",
            delivery="always",
            expected_version=nomination["version"],
            approval={"kind": "user_direct"},
            request_id=uuid4(),
        )

        with pytest.raises(MashuError, match="no longer pending"):
            nominations.decline(
                cur, nomination["nomination_id"], actor="user", reason="changed my mind"
            )
        conn.rollback()


def test_two_connections_replaying_one_memory_change_create_one_result(committing_dsn, two):
    with psycopg.connect(committing_dsn, row_factory=dict_row) as setup, setup.cursor() as cur:
        memory = memories.remember(
            cur, content="the exporter records its checksum in the signed index", actor="user"
        )
        detail = memories.memory_details(cur, memory["memory_id"])
        proposal = memory_changes.propose(
            cur,
            target_memory_id=memory["memory_id"],
            target_revision_id=detail["current_revision_id"],
            target_updated_at=detail["updated_at"],
            operation="retire",
            retirement_kind="out_of_scope",
            retire_reason="the exporter now records its checksum in the index",
            evidence=[
                {
                    "kind": "ledger",
                    "id": str(memory["evidence"][0]),
                    "observation": "the export job now includes the checksum",
                }
            ],
            actor="agent",
        )

    first, second = two
    request_id = uuid4()
    approval = {"kind": "user_direct"}
    with first.cursor() as cur:
        result = memory_changes.apply(
            cur,
            proposal["change_id"],
            version=1,
            request_id=request_id,
            approval=approval,
            actor="user",
        )

    started, completed = Event(), Event()
    replay_result: list[dict] = []
    failures: list[BaseException] = []

    def replay() -> None:
        started.set()
        try:
            with second.cursor() as cur:
                replay_result.append(
                    memory_changes.apply(
                        cur,
                        proposal["change_id"],
                        version=1,
                        request_id=request_id,
                        approval=approval,
                        actor="user",
                    )
                )
        except BaseException as error:
            failures.append(error)
        finally:
            completed.set()

    worker = Thread(target=replay)
    worker.start()
    assert started.wait(timeout=1)
    assert not completed.wait(timeout=0.05)
    first.commit()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not failures
    assert replay_result == [result]

    with psycopg.connect(committing_dsn, row_factory=dict_row) as verify, verify.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM event_log WHERE event_type = 'memory_retired' "
            "AND memory_id = %s",
            (memory["memory_id"],),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT status FROM memory_change WHERE change_id = %s", (proposal["change_id"],)
        )
        assert cur.fetchone()["status"] == "applied"
