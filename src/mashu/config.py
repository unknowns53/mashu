"""Tunables, read from the environment at call time.

Read per call rather than at import so tests can point them elsewhere, and so
a long-lived MCP server picks up a changed value on the next operation.
"""

from __future__ import annotations

import os

#: Hard ceiling on what one session's bootstrap may carry, in estimated
#: tokens (specification 5.2). A seat count, not a bill: admission past it is
#: refused unless something else retires or steps down to guard.
DEFAULT_CAPACITY = 2000

#: Similarity at which two pains count as the same hole (4.1). Conservative
#: on purpose; calibrated against real entries once there are any.
DEFAULT_MATCH_THRESHOLD = 0.45

#: Similarity at which a row is worth showing to the reporter at all.
SHOW_THRESHOLD = 0.25

#: How long a trace lives (4.2).
DEFAULT_TRACE_TTL_DAYS = 30

#: Ceiling on a temporary context window (7). A constraint, not a default.
TEMPORARY_MAX_DAYS = 14


def capacity() -> int:
    return int(os.environ.get("MASHU_CAPACITY") or DEFAULT_CAPACITY)


def match_threshold() -> float:
    return float(os.environ.get("MASHU_MATCH_THRESHOLD") or DEFAULT_MATCH_THRESHOLD)


def trace_ttl_days() -> int:
    return int(os.environ.get("MASHU_TRACE_TTL_DAYS") or DEFAULT_TRACE_TTL_DAYS)
