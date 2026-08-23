"""The transition table on its own, with no database involved."""

import pytest

from mashu.errors import ActivePointerError, TransitionError
from mashu.models import VersionStatus
from mashu.transitions import (
    ALLOWED_TRANSITIONS,
    STATUSES_ALLOWED_AS_ACTIVE,
    check_can_be_active,
    check_transition,
    is_terminal,
)

S = VersionStatus


def test_every_status_appears_in_the_table():
    assert set(ALLOWED_TRANSITIONS) == set(VersionStatus)


@pytest.mark.parametrize("target", [S.SUPERSEDED, S.DISPROVEN, S.DORMANT, S.COMPLETED])
def test_a_candidate_can_reach_every_outcome(target):
    check_transition(S.CANDIDATE, target)


@pytest.mark.parametrize("status", [S.SUPERSEDED, S.DISPROVEN, S.DORMANT])
def test_outcomes_are_absorbing(status):
    """Specification 12 forbids reviving a version; a Restore is a new one."""
    assert is_terminal(status)
    with pytest.raises(TransitionError, match="terminal"):
        check_transition(status, S.CANDIDATE)


def test_a_finished_task_can_still_be_replaced_or_disproven():
    check_transition(S.COMPLETED, S.SUPERSEDED)
    check_transition(S.COMPLETED, S.DISPROVEN)


def test_a_finished_task_does_not_go_dormant():
    with pytest.raises(TransitionError, match="cannot move completed to dormant"):
        check_transition(S.COMPLETED, S.DORMANT)


def test_a_status_change_has_to_move_the_version():
    with pytest.raises(TransitionError, match="already"):
        check_transition(S.CANDIDATE, S.CANDIDATE)


def test_only_candidate_and_completed_may_be_active():
    assert STATUSES_ALLOWED_AS_ACTIVE == {S.CANDIDATE, S.COMPLETED}
    check_can_be_active(S.CANDIDATE)
    check_can_be_active(S.COMPLETED)


@pytest.mark.parametrize("status", [S.SUPERSEDED, S.DISPROVEN, S.DORMANT])
def test_a_retired_version_cannot_be_active(status):
    with pytest.raises(ActivePointerError):
        check_can_be_active(status)
