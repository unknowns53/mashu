"""The commit gate sorts by danger, not by plausibility (specification 17)."""

from __future__ import annotations

import pytest

from mashu.gate import CommitDecision, classify
from mashu.models import (
    EntityStatus,
    MemoryType,
    ProposalOperation,
    SourceType,
    VersionStatus,
)


@pytest.mark.parametrize(
    "type_,expected",
    [
        (MemoryType.PREFERENCE, CommitDecision.AUTO),
        (MemoryType.FACT, CommitDecision.CANDIDATE),
        (MemoryType.INTERPRETATION, CommitDecision.CANDIDATE),
        (MemoryType.HYPOTHESIS, CommitDecision.CANDIDATE),
        (MemoryType.STATE, CommitDecision.CANDIDATE),
    ],
)
def test_types_the_section_lists(type_, expected):
    ruling = classify(operation=ProposalOperation.CREATE, type=type_, source_type=SourceType.AGENT)
    assert ruling.decision is expected


@pytest.mark.parametrize("type_", [MemoryType.OBSERVATION, MemoryType.DECISION, MemoryType.TASK])
def test_unlisted_types_are_held_not_applied(type_):
    """A gate that fails open is not a safety classification."""
    ruling = classify(operation=ProposalOperation.CREATE, type=type_, source_type=SourceType.AGENT)
    assert ruling.decision is CommitDecision.CANDIDATE
    assert "not classified" in ruling.reason


def test_user_stated_change_is_applied():
    ruling = classify(
        operation=ProposalOperation.CREATE,
        type=MemoryType.FACT,
        source_type=SourceType.USER,
    )
    assert ruling.decision is CommitDecision.AUTO


def test_task_completion_is_applied():
    ruling = classify(
        operation=ProposalOperation.CHANGE_STATUS,
        type=MemoryType.TASK,
        target_status=VersionStatus.COMPLETED,
    )
    assert ruling.decision is CommitDecision.AUTO


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": ProposalOperation.MERGE},
        {"operation": ProposalOperation.RESTORE},
        {
            "operation": ProposalOperation.CHANGE_STATUS,
            "target_status": VersionStatus.DISPROVEN,
        },
        {
            "operation": ProposalOperation.CREATE,
            "type": MemoryType.FACT,
            "entity_status": EntityStatus.PROVISIONAL,
        },
        {
            "operation": ProposalOperation.UPDATE_VERSION,
            "type": MemoryType.FACT,
            "switches_active": True,
        },
    ],
)
def test_dangerous_operations_wait_for_the_user(kwargs):
    assert classify(**kwargs).decision is CommitDecision.HUMAN_REVIEW


def test_danger_outranks_the_user_saying_so():
    """Disproving waits even when the user is the one who said it.

    A wrong disproval removes knowledge, and nobody notices an absence.
    """
    ruling = classify(
        operation=ProposalOperation.CHANGE_STATUS,
        type=MemoryType.FACT,
        source_type=SourceType.USER,
        target_status=VersionStatus.DISPROVEN,
    )
    assert ruling.decision is CommitDecision.HUMAN_REVIEW


def test_preference_does_not_launder_a_merge():
    ruling = classify(operation=ProposalOperation.MERGE, type=MemoryType.PREFERENCE)
    assert ruling.decision is CommitDecision.HUMAN_REVIEW


def test_every_ruling_carries_a_reason():
    for type_ in MemoryType:
        ruling = classify(
            operation=ProposalOperation.CREATE, type=type_, source_type=SourceType.AGENT
        )
        assert ruling.reason
