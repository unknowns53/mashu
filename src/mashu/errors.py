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


class DuplicateProposalError(MashuError):
    """An equivalent proposal has already been made (specification 15.1).

    Either it is still waiting for review, or it was turned down. Both are
    worth stopping for, and the second is worth more: a proposer that never
    hears about a rejection will make the same proposal again, and the
    reviewer will spend the same minutes turning it down again.

    Carries the proposals it found, with their decision_reason, so the caller
    can look at them and decide. Blocking is not the point; the point is that
    the proposer sees what has already been ruled on. A caller that has looked
    and judged the change genuinely different can say so and propose anyway.
    """

    def __init__(self, message: str, existing: list[dict]) -> None:
        super().__init__(message)
        self.existing = existing

    @property
    def rejected(self) -> list[dict]:
        """The ones a reviewer already turned down."""
        return [row for row in self.existing if row["status"] == "rejected"]
