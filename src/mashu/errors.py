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
