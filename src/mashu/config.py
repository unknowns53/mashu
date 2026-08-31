"""Tunables, read from the environment at call time.

Read per call rather than at import so tests can point them elsewhere, and so
a long-lived MCP server picks up a changed value on the next operation.
"""

from __future__ import annotations

import os

#: Hard ceiling on everything one session's bootstrap carries: the memory
#: seats, the project state share and the temporary share together (v3 8).
#: Nothing is checked against this number — each share is refused at its own
#: entrance — so it is what the three of them come to when all three are full,
#: and the reading that says an entrance has stopped holding.
DEFAULT_TOTAL_CAPACITY = 4000

#: Hard ceiling on the memories alone, in estimated tokens (5.2). A seat
#: count, not a bill: admission past it is refused unless something else
#: retires or steps down to guard.
DEFAULT_CAPACITY = 2000

#: Hard ceiling on the always layer alone, inside the whole-opening one above
#: (5.2). What it protects is the scopes' share: every token delivered to every
#: session is one no scope can ever use.
DEFAULT_ALWAYS_CAPACITY = 800

#: Hard ceiling on the Project State share of the opening, in estimated
#: tokens (v3 5.3, 8). Separate from the memory ceiling on purpose: the two
#: subsystems never borrow each other's room, so a week of busy work cannot
#: quietly evict the rules that were admitted against evidence.
DEFAULT_PROJECT_CAPACITY = 1600

#: Hard ceiling on the temporary share of the opening (v3 8). In v2 these rode
#: inside the memory seat count; giving them a room of their own is what stops
#: a fortnight of expiring conditions from evicting a rule that was admitted
#: against evidence, and stops the rules from leaving no room for this week's
#: outage either.
DEFAULT_TEMPORARY_CAPACITY = 400

#: How long a task's lease runs from its last sign of activity (v3 7). Long
#: enough for a queue wait or a review cycle to be silence rather than
#: absence; short enough that a forgotten task stops being pushed.
DEFAULT_TASK_LEASE_DAYS = 14

#: Similarity at which two pains count as the same hole (4.1). Conservative
#: on purpose; calibrated against real entries once there are any.
DEFAULT_MATCH_THRESHOLD = 0.45

#: Similarity at which a row is worth showing to the reporter at all.
SHOW_THRESHOLD = 0.25

#: How long a trace lives (4.2).
DEFAULT_TRACE_TTL_DAYS = 30

#: Ceiling on a temporary context window (7). A constraint, not a default.
TEMPORARY_MAX_DAYS = 14


def total_capacity() -> int:
    return int(os.environ.get("MASHU_TOTAL_CAPACITY") or DEFAULT_TOTAL_CAPACITY)


def capacity() -> int:
    return int(os.environ.get("MASHU_CAPACITY") or DEFAULT_CAPACITY)


def always_capacity() -> int:
    return int(os.environ.get("MASHU_ALWAYS_CAPACITY") or DEFAULT_ALWAYS_CAPACITY)


def project_capacity() -> int:
    return int(os.environ.get("MASHU_PROJECT_CAPACITY") or DEFAULT_PROJECT_CAPACITY)


def temporary_capacity() -> int:
    return int(os.environ.get("MASHU_TEMPORARY_CAPACITY") or DEFAULT_TEMPORARY_CAPACITY)


def task_lease_days() -> int:
    return int(os.environ.get("MASHU_TASK_LEASE_DAYS") or DEFAULT_TASK_LEASE_DAYS)


def match_threshold() -> float:
    return float(os.environ.get("MASHU_MATCH_THRESHOLD") or DEFAULT_MATCH_THRESHOLD)


def trace_ttl_days() -> int:
    return int(os.environ.get("MASHU_TRACE_TTL_DAYS") or DEFAULT_TRACE_TTL_DAYS)
