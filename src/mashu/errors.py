"""Exception types raised by the Mashu service."""

from __future__ import annotations


class MashuError(Exception):
    """A request that cannot be carried out as asked."""


class RefusedError(MashuError):
    """A write the layer refuses to accept."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RetiredConflictError(MashuError):
    """A write that repeats something already withdrawn, stopped to be read."""

    def __init__(self, reason: str, tombstones: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.tombstones = tombstones


class OverLimitError(RefusedError):
    """A current-state field longer than the store will hold (v3 5.3)."""

    def __init__(self, field: str, limit: int, actual: int, *, unit: str = "chars"):
        super().__init__(
            f"{field} is {actual} {unit} and the limit is {limit}; "
            "put the long form where its original lives and reference it instead"
        )
        self.field = field
        self.limit = limit
        self.actual = actual


class StaleStateError(MashuError):
    """A replacement written against a state somebody has already replaced."""

    def __init__(self, reason: str, current: dict[str, object]):
        super().__init__(reason)
        self.reason = reason
        self.current = current


class ProjectBudgetError(RefusedError):
    """A state that would push the Project State share past its ceiling."""

    def __init__(self, reason: str, breakdown: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.breakdown = breakdown


class DuplicateTaskError(MashuError):
    """A task that reads like one already open in the same project (v3 5.2)."""

    def __init__(self, reason: str, candidates: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.candidates = candidates


class UnknownProjectError(MashuError):
    """A project nobody has opened, answered with the ones that exist."""


class ClosedTaskError(MashuError):
    """A write to a task a person has already ended (v3 6)."""
