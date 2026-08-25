"""Append-only, enforced by the database rather than promised by the code.

Three tables carry the trigger, and each carries it for its own reason. The
ledger is what memories cite, so a row that could be rewritten would let an
active claim silently change what it stands on. The revision history is the
only record of what a rule used to say. The event log is the only account of
who did any of it, and an audit trail that can be edited is a narrative.
"""

from __future__ import annotations

import psycopg
import pytest

from mashu import ledger, memories


def test_the_ledger_refuses_to_be_rewritten(cur):
    ledger.report_pain(
        cur,
        kind="incident",
        what="the wrong branch was deployed",
        prevention="check the branch before deploying",
        actor="agent",
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("UPDATE ledger SET what = 'something else'")


def test_the_ledger_refuses_to_be_emptied(cur):
    ledger.report_pain(
        cur,
        kind="incident",
        what="the wrong branch was deployed",
        prevention="check the branch before deploying",
        actor="agent",
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("DELETE FROM ledger")


def test_a_revision_cannot_be_edited(cur):
    memories.remember(cur, content="answer in the language that was asked", actor="user")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("UPDATE memory_revision SET content = 'never said that'")


def test_a_revision_cannot_be_deleted(cur):
    memories.remember(cur, content="answer in the language that was asked", actor="user")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("DELETE FROM memory_revision")


def test_the_event_log_cannot_be_edited(cur):
    memories.remember(cur, content="answer in the language that was asked", actor="user")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("UPDATE event_log SET actor = 'somebody else'")


def test_the_event_log_cannot_be_deleted(cur):
    memories.remember(cur, content="answer in the language that was asked", actor="user")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        cur.execute("DELETE FROM event_log")
