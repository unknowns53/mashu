"""Accidents, counted and split by cause (27.5, 30 段 D)."""

from __future__ import annotations

import pytest

from mashu import incidents
from mashu.incidents import IncidentError
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


# --------------------------------------------------------------------------
# the trial window (27.5, 30 段 E)
# --------------------------------------------------------------------------
def test_a_window_bounds_what_the_tally_counts(cur, scope_id):
    """27.5 counts accidents inside two weeks of running, not accidents ever.

    Without a mark for when the window opened, the count afterwards is over
    whatever has accumulated, and the exercise cannot say what it measured.
    """
    incidents.record(
        cur,
        kind=IncidentKind.MISSED,
        cause=IncidentCause.BOOTSTRAP,
        note="before the window; the opening was short",
        recorded_by="user",
        scope_id=scope_id,
        occurred_at="2026-01-01T09:00:00+09:00",
    )
    incidents.open_trial(cur, note="native memory switched off", actor="user")
    incidents.record(
        cur,
        kind=IncidentKind.MISSED,
        cause=IncidentCause.PULL,
        note="inside the window; the index was there and nobody pulled",
        recorded_by="user",
        scope_id=scope_id,
    )

    since = incidents.current_trial(cur)["opened_at"]
    inside = incidents.tally(cur, since=since)
    assert [row["cause"] for row in inside] == [str(IncidentCause.PULL)]
    assert sum(row["n"] for row in incidents.tally(cur)) == 2


def test_two_windows_cannot_overlap(cur):
    """Which window a tally belongs to is the whole output; it cannot be ambiguous."""
    incidents.open_trial(cur, note="native memory switched off", actor="user")
    with pytest.raises(IncidentError, match="close it before opening another"):
        incidents.open_trial(cur, note="again", actor="user")

    incidents.close_trial(cur, note="two weeks up", actor="user")
    assert incidents.current_trial(cur) is None
    incidents.open_trial(cur, note="a second run", actor="user")
    assert incidents.current_trial(cur) is not None


def test_a_window_needs_an_account_of_what_was_switched_off(cur):
    """Same rule the accident record follows: a row nobody can read later."""
    with pytest.raises(IncidentError, match="say what is being switched off"):
        incidents.open_trial(cur, note="   ", actor="user")


def test_closing_without_a_window_is_refused(cur):
    with pytest.raises(IncidentError, match="no window is open"):
        incidents.close_trial(cur, note="", actor="user")
