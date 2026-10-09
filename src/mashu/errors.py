"""Exception types raised by the Mashu service."""

from __future__ import annotations


class MashuError(Exception):
    """A request that cannot be carried out as asked."""


class RefusedError(MashuError):
    """A write the layer refuses to accept."""

    def __init__(
        self, reason: str, *, field: str | None = None, span: tuple[int, int] | None = None
    ):
        super().__init__(reason)
        self.reason = reason
        #: The argument the refused text came from, when the check knew it.
        self.field = field
        #: Where in that argument's text the refusal applies, as a slice.
        self.span = span


class MalformedRequestError(MashuError):
    """A request whose shape is wrong in one or more ways, all reported together."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class RetiredConflictError(MashuError):
    """A write that repeats something already withdrawn, stopped to be read."""

    def __init__(self, reason: str, tombstones: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.tombstones = tombstones


class OverLimitError(RefusedError):
    """A current-state field, card, or detail longer than the store will hold."""

    def __init__(
        self,
        field: str,
        limit: int,
        actual: int,
        *,
        unit: str = "chars",
        advice: str | None = None,
        breakdown: dict[str, int] | None = None,
    ):
        advice = advice or "put the long form where its original lives and reference it instead"
        over_by = max(0, actual - limit)
        if breakdown is None:
            reason = f"{field} is {actual} {unit} and the limit is {limit}; {advice}"
        else:
            parts = ", ".join(f"{part} {tokens}" for part, tokens in breakdown.items())
            reason = (
                f"{field} is {actual}/{limit} {unit}, {over_by} over; reduce it by at least "
                f"{over_by}: {advice}. Contributions: {parts}."
            )
        super().__init__(reason)
        self.field = field
        self.limit = limit
        self.actual = actual
        self.unit = unit
        self.over_by = over_by
        self.breakdown = breakdown


class StaleStateError(MashuError):
    """A replacement written against a state somebody has already replaced."""

    def __init__(self, reason: str, current: dict[str, object]):
        super().__init__(reason)
        self.reason = reason
        self.current = current


class ProjectBudgetError(RefusedError):
    """A card that would push the active-task share past its ceiling."""

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
