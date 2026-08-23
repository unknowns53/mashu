"""The deterministic part of status handling (specification 12).

Nothing here touches the database and nothing here interprets content. These
are the rules the system may apply on its own; every judgement about whether a
memory is true belongs to the user.

On what status an adopted version carries
-----------------------------------------
Specification 12 draws an adopted state between candidate and the outcomes, but
specification 11 does not list one, because specification 10 removed active
from the status vocabulary so that the pointer is the only place activeness
lives. That leaves a gap the written specification does not close: an approved
version that is currently the truth needs some status, and none of superseded,
disproven or dormant fits.

Reading the three sections together, the resolution that keeps a single source
of truth is to let status carry only the outcome a version has reached, and to
let the pointer carry adoption:

    candidate  = no outcome yet; may or may not be the pointer target
    completed  = finished, and still the truth worth retrieving
    superseded, disproven, dormant = no longer the truth

So a candidate that the pointer targets is the active version, and a candidate
the pointer does not target is waiting for review. Adding an adopted status
instead would recreate exactly the duplication specification 10 removed, since
an adopted version and the pointer target would always have to agree.

completed stays eligible to be active on purpose. Specification 1 names "a
finished task treated as unfinished" as a problem to solve, so a finished task
has to keep surfacing in retrieval with its finished status visible.
"""

from mashu.errors import ActivePointerError, TransitionError
from mashu.models import VersionStatus

_C = VersionStatus

#: What a version in a given status may become.
#:
#: superseded, disproven and dormant are absorbing. Specification 12 forbids
#: reviving an existing version, so coming back from any of them is a new
#: version created by a Restore, never an edit here.
ALLOWED_TRANSITIONS: dict[VersionStatus, frozenset[VersionStatus]] = {
    _C.CANDIDATE: frozenset({_C.SUPERSEDED, _C.DISPROVEN, _C.DORMANT, _C.COMPLETED}),
    # A finished task can still be replaced by a newer one, or turn out to have
    # been wrong. It does not go dormant: nothing about it is pending re-use.
    _C.COMPLETED: frozenset({_C.SUPERSEDED, _C.DISPROVEN}),
    _C.SUPERSEDED: frozenset(),
    _C.DISPROVEN: frozenset(),
    _C.DORMANT: frozenset(),
}

#: The statuses a version may hold while active_version points at it.
STATUSES_ALLOWED_AS_ACTIVE: frozenset[VersionStatus] = frozenset({_C.CANDIDATE, _C.COMPLETED})

#: Statuses from which nothing further follows.
TERMINAL_STATUSES: frozenset[VersionStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def is_terminal(status: VersionStatus) -> bool:
    """Whether the status admits no further change."""
    return VersionStatus(status) in TERMINAL_STATUSES


def can_transition(current: VersionStatus, target: VersionStatus) -> bool:
    """Whether the status change is permitted. A no-op change is not."""
    return VersionStatus(target) in ALLOWED_TRANSITIONS[VersionStatus(current)]


def check_transition(current: VersionStatus, target: VersionStatus) -> None:
    """Raise TransitionError unless the status change is permitted."""
    current, target = VersionStatus(current), VersionStatus(target)
    if current == target:
        raise TransitionError(f"version is already {current}; a status change has to move it")
    if not can_transition(current, target):
        allowed = sorted(ALLOWED_TRANSITIONS[current])
        if not allowed:
            raise TransitionError(
                f"{current} is terminal; bring the content back with a new "
                f"version instead of changing this one"
            )
        raise TransitionError(f"cannot move {current} to {target}; allowed: {', '.join(allowed)}")


def can_be_active(status: VersionStatus) -> bool:
    """Whether active_version may point at a version in this status."""
    return VersionStatus(status) in STATUSES_ALLOWED_AS_ACTIVE


def check_can_be_active(status: VersionStatus) -> None:
    """Raise ActivePointerError unless the status may be the pointer target."""
    if not can_be_active(status):
        allowed = ", ".join(sorted(STATUSES_ALLOWED_AS_ACTIVE))
        raise ActivePointerError(
            f"a {VersionStatus(status)} version cannot be active; only {allowed} may be"
        )
