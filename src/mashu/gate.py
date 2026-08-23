"""Which changes the system may apply on its own (specification 17).

The gate does not judge whether a memory is true. It sorts changes by how much
damage applying one without a human would do, and nothing else. Three outcomes:
apply now, hold as a candidate, or wait for the user.

Two readings the specification leaves open are settled here, both towards the
cautious side.

Types the section does not list. Section 17 names preference, fact,
interpretation, hypothesis and state, which leaves observation, decision and
task unclassified. An unlisted type is held as a candidate rather than applied,
because a gate that fails open stops being a safety classification.

What counts as an active switch. Adopting the version a proposal itself carries
is the ordinary end of update_version, and reading it as the reviewable "active
switch" would put every preference change behind a review and empty the auto
commit line of its meaning. So the reviewable switch is the standalone one:
moving the pointer to a version the proposal did not write.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple

from mashu.models import (
    EntityStatus,
    MemoryType,
    ProposalOperation,
    SourceType,
    VersionStatus,
)


class CommitDecision(StrEnum):
    """The three lines of section 17."""

    AUTO = "auto"
    CANDIDATE = "candidate"
    HUMAN_REVIEW = "human_review"


class GateRuling(NamedTuple):
    """A decision together with the rule that produced it.

    The reason is stored on the proposal and shown in the review queue. Without
    it the user cannot tell a held proposal from a dangerous one.
    """

    decision: CommitDecision
    reason: str


#: Types section 17 puts on the candidate line.
CANDIDATE_TYPES = frozenset(
    {
        MemoryType.FACT,
        MemoryType.INTERPRETATION,
        MemoryType.HYPOTHESIS,
        MemoryType.STATE,
    }
)

#: Types section 17 never mentions. Held as candidates; see the module note.
UNCLASSIFIED_TYPES = frozenset(
    {
        MemoryType.OBSERVATION,
        MemoryType.DECISION,
        MemoryType.TASK,
    }
)


def classify(
    *,
    operation: ProposalOperation,
    type: MemoryType | None = None,
    source_type: SourceType | None = None,
    entity_status: EntityStatus = EntityStatus.ACTIVE,
    target_status: VersionStatus | None = None,
    switches_active: bool = False,
) -> GateRuling:
    """Sort one proposed change into a commit line.

    The dangerous cases are tested first, so a change that is dangerous in one
    respect cannot be waved through by being ordinary in another. Disproving a
    memory waits for the user even when the user is the one who said it: the
    cost of a wrong disproval is a piece of knowledge that no longer surfaces
    and whose absence nobody notices.
    """
    operation = ProposalOperation(operation)
    type = MemoryType(type) if type is not None else None
    source_type = SourceType(source_type) if source_type is not None else None
    entity_status = EntityStatus(entity_status)
    target_status = VersionStatus(target_status) if target_status is not None else None

    if operation is ProposalOperation.MERGE:
        return GateRuling(CommitDecision.HUMAN_REVIEW, "entity merge")
    if operation is ProposalOperation.RESTORE:
        return GateRuling(CommitDecision.HUMAN_REVIEW, "restore")
    if target_status is VersionStatus.DISPROVEN:
        return GateRuling(CommitDecision.HUMAN_REVIEW, "disproving a version")
    if entity_status is EntityStatus.PROVISIONAL:
        return GateRuling(
            CommitDecision.HUMAN_REVIEW,
            "entity created despite a title similarity above the threshold",
        )
    if switches_active:
        return GateRuling(CommitDecision.HUMAN_REVIEW, "switching the active version")

    if (
        operation is ProposalOperation.CHANGE_STATUS
        and target_status is VersionStatus.COMPLETED
        and type is MemoryType.TASK
    ):
        return GateRuling(CommitDecision.AUTO, "task completion")
    if source_type is SourceType.USER:
        return GateRuling(CommitDecision.AUTO, "change stated by the user")
    if type is MemoryType.PREFERENCE:
        return GateRuling(CommitDecision.AUTO, "preference")

    if type in CANDIDATE_TYPES:
        return GateRuling(CommitDecision.CANDIDATE, f"{type} is reviewed before it is trusted")
    if type in UNCLASSIFIED_TYPES:
        return GateRuling(
            CommitDecision.CANDIDATE,
            f"{type} is not classified by section 17; held rather than applied",
        )
    return GateRuling(
        CommitDecision.CANDIDATE,
        f"no rule matched operation {operation}; held rather than applied",
    )
