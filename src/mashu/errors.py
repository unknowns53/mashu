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
