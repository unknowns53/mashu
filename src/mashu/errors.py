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
