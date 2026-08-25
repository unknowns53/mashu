"""Two writers at once (specification 9).

Several MCP processes and a command line write to one store, so the checks
that read before they write have to be serialised or they are not checks. No
threads here: one connection begins a transaction and stops holding the lock
open, a second sets a short lock_timeout and is made to give up. That is
deterministic, and a sleeping thread racing a real one is not.

Both connections roll back at the end, so what a test writes is only ever seen
by its own two connections.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from mashu import ledger, memories, nominations, scopes
from mashu.errors import MashuError

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
    """Checking a ceiling and taking a place under it must be one act.

    Read the totals concurrently and both writers find room for the last seat,
    both sit down, and the opening is over a ceiling that was checked twice.
    """
    first, second = two
    with first.cursor() as writing:
        memories.remember(writing, content=RULE, actor="user")

        with impatient(second) as waiting, pytest.raises(psycopg.errors.LockNotAvailable):
            memories.remember(waiting, content=OTHER_RULE, actor="user")


def test_two_pains_cannot_be_matched_against_each_other_at_once(two):
    """One hole reported twice at once would produce two candidates.

    Each report matches only what the other has not written yet, so each finds
    nothing waiting and files its own. The queue then holds two rows saying one
    thing, in a queue whose whole premise is that it holds a few a week.
    """
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
    """Admitting and declining both read 'pending' and then write.

    Without the row lock the two reviewers each see a live candidate, and the
    queue ends up carrying half of each decision.
    """
    with psycopg.connect(committing_dsn, row_factory=dict_row) as setup, setup.cursor() as cur:
        scope = scopes.create_scope(cur, name="a scope for the race", actor="user")
        nomination = nominations.nominate_user_explicit(
            cur, content=RULE, actor="agent", scope_id=scope["scope_id"]
        )["nomination"]

    first, second = two
    with first.cursor() as deciding:
        nominations.admit(deciding, nomination["nomination_id"], actor="user", delivery="always")

        with impatient(second) as waiting, pytest.raises(psycopg.errors.LockNotAvailable):
            nominations.decline(
                waiting, nomination["nomination_id"], actor="user", reason="not worth a seat"
            )


def test_a_candidate_decided_once_is_not_decided_again(committing_dsn):
    """The sequential half of the same rule, where no lock is involved at all."""
    with psycopg.connect(committing_dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        nomination = nominations.nominate_user_explicit(cur, content=OTHER_RULE, actor="agent")[
            "nomination"
        ]
        nominations.admit(cur, nomination["nomination_id"], actor="user", delivery="always")

        with pytest.raises(MashuError, match="no longer pending"):
            nominations.decline(
                cur, nomination["nomination_id"], actor="user", reason="changed my mind"
            )
        conn.rollback()
