"""Domain errors.

Every error here means the caller asked for something the knowledge state does
not allow. Nothing in this module reports an infrastructure failure; those come
out of psycopg unchanged.
"""


class MashuError(Exception):
    """Base class for every rule the memory layer enforces."""


class NotFoundError(MashuError):
    """A scope, entity or version that the caller named does not exist."""


class TransitionError(MashuError):
    """A status change that the transition table forbids (specification 12)."""


class ActivePointerError(MashuError):
    """An attempt to point active_version at a version that cannot hold it."""


class ConcurrentUpdateError(MashuError):
    """based_on_version no longer matches latest_version (specification 24).

    The caller has to re-read the entity and propose again; the layer never
    merges on the caller's behalf.
    """


class MergeError(MashuError):
    """A merge that cannot be carried out as asked (specification 20.2)."""


class ProposalError(MashuError):
    """A proposal cannot be decided the way the caller asked."""


class DuplicatePendingError(MashuError):
    """An equivalent proposal is already waiting for review (specification 15.1).

    Carries the proposals it found so the caller can look at them and decide.
    Blocking is not the point; the point is that the proposer sees what is
    already in the queue. A caller that has looked and judged the change
    genuinely different can say so and propose anyway.
    """

    def __init__(self, message: str, existing: list[dict]) -> None:
        super().__init__(message)
        self.existing = existing
