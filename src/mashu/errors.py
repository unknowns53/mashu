"""Errors the layer raises on purpose."""

from __future__ import annotations


class MashuError(Exception):
    """A request that cannot be carried out as asked."""


class RefusedError(MashuError):
    """A write the layer refuses to accept.

    Refusal, not rewriting: editing content on its way in would leave text
    nobody chose, and the message says what would make the request pass.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RetiredConflictError(MashuError):
    """A write that repeats something already withdrawn, stopped to be read.

    Not a refusal: the person typing is the one authority that may step over a
    retirement (5.3), so this is a halt rather than a no. What it defends
    against is stepping over one unknowingly — the reason a claim was
    withdrawn is exactly what the person re-entering it needs, and it is held
    in a row they have no reason to go looking at.

    The tombstones travel on the exception because the caller has to print
    them: an error saying only that something collided sends the reader to
    `mashu memories --retired` to find out what.
    """

    def __init__(self, reason: str, tombstones: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.tombstones = tombstones


class OverLimitError(RefusedError):
    """A current-state field longer than the store will hold (v3 5.3).

    The limit is the mechanism rather than a formality: an instruction to
    write concisely does not control volume, and what overflows a small
    delivered state is not one long field but the next session's attention.
    The field and both numbers travel because the writer has to cut something,
    and cannot see from where they stand what the ceiling was.
    """

    def __init__(self, field: str, limit: int, actual: int, *, unit: str = "chars"):
        super().__init__(
            f"{field} is {actual} {unit} and the limit is {limit}; "
            "put the long form where its original lives and reference it instead"
        )
        self.field = field
        self.limit = limit
        self.actual = actual


class StaleStateError(MashuError):
    """A replacement written against a state somebody has already replaced.

    The current state travels on the exception because rejecting is only half
    an answer: the caller is holding a rewrite of text that no longer exists,
    and what it needs in order to redo the work is the version that won. A
    silent last-writer-wins would instead delete a parallel session's work
    with nothing anywhere saying so (v3 5.3).
    """

    def __init__(self, reason: str, current: dict[str, object]):
        super().__init__(reason)
        self.reason = reason
        self.current = current


class ProjectBudgetError(RefusedError):
    """A state that would push the Project State share past its ceiling.

    Refused rather than truncated, and never demoted to something searchable:
    there is nothing to search here either (v3 8). The breakdown rides along
    because the exits — shrink another state, let a task go dormant, have the
    user close one — all require knowing which task is spending the room.
    """

    def __init__(self, reason: str, breakdown: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.breakdown = breakdown


class DuplicateTaskError(MashuError):
    """A task that reads like one already open in the same project (v3 5.2).

    Not a refusal: the agent may genuinely be starting a second piece of work
    that sounds like the first, and it says so with `force`. What this stops
    is the accident — a task created because a search missed one, after which
    the state for one piece of work grows in two places and neither is whole.
    The candidates travel on the exception because continuing an existing task
    is the answer nine times in ten, and it needs their ids.
    """

    def __init__(self, reason: str, candidates: list[dict[str, object]]):
        super().__init__(reason)
        self.reason = reason
        self.candidates = candidates


class UnknownProjectError(MashuError):
    """A project nobody has opened, answered with the ones that exist.

    A caller who mistyped cannot see the project ledger from where it stands,
    and a task filed under a project that was silently created for it would
    split one body of work state in half.
    """


class ClosedTaskError(MashuError):
    """A write to a task a person has already ended (v3 6).

    Closing is the one judgement in this subsystem reserved for a person, so
    an agent writing on past it would take that decision back without saying
    so. Reopening is the same person's key-press.
    """
