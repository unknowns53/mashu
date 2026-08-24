"""Accidents, counted and split by cause (27.5, 30 段 D)."""

from __future__ import annotations

import pytest

from mashu import incidents
from mashu.models import EventType, IncidentCause, IncidentKind


def test_a_cause_may_not_cross_between_the_two_kinds(cur):
    """The kinds are opposite directions of failure, not a single severity axis.

    missed is knowledge the store held and the session never got. stale is
    knowledge the store had withdrawn and handed over anyway. A cause that
    could sit under either would put both directions in one column, which is
    the blur the split exists to prevent.
    """
    with pytest.raises(incidents.IncidentError):
        incidents.record(
            cur,
            kind=IncidentKind.MISSED,
            cause=IncidentCause.FILTER,
            note="a temporary context outlived its window",
            recorded_by="user",
        )


def test_an_accident_with_no_account_is_refused(cur):
    """27.5 counts occurrences by cause; a row nobody can sort has no use."""
    with pytest.raises(incidents.IncidentError):
        incidents.record(
            cur,
            kind=IncidentKind.MISSED,
            cause=IncidentCause.PULL,
            note="   ",
            recorded_by="user",
        )


def test_capture_is_a_cause_of_its_own(cur):
    """段 E adds it: before unattended capture nobody could have missed writing
    something down, so the old two-way split had nowhere to put it."""
    made = incidents.record(
        cur,
        kind=IncidentKind.MISSED,
        cause=IncidentCause.CAPTURE,
        note="the decision was never proposed; the session it came from was held for routing",
        recorded_by="user",
    )
    assert made["kind"] == "missed"
    assert made["cause"] == "capture"

    cur.execute(
        "SELECT count(*) AS n FROM event_log WHERE event_type = %s",
        (str(EventType.INCIDENT_RECORDED),),
    )
    assert cur.fetchone()["n"] >= 1

    counted = {(r["kind"], r["cause"]): r["n"] for r in incidents.tally(cur)}
    assert counted[("missed", "capture")] == 1


def test_every_cause_says_where_it_sends_you(cur):
    """A tally without that reads as a score rather than as work to do."""
    for causes in incidents.CAUSES.values():
        for cause in causes:
            assert incidents.LEADS_TO[cause]
