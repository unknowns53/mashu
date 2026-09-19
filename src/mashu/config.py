"""Read Mashu configuration from the environment."""

from __future__ import annotations

import os

#: Overall limit for memory, project-state, and temporary-context payloads (v3 8).
DEFAULT_TOTAL_CAPACITY = 4000

#: Hard ceiling on the memories alone, in estimated tokens (5.2).
DEFAULT_CAPACITY = 2000

#: Hard ceiling on the always layer alone, inside the whole-opening one above (5.2).
DEFAULT_ALWAYS_CAPACITY = 800

#: Hard ceiling on each scope's Project State share, in estimated tokens (v3 5.3, 8).
DEFAULT_PROJECT_CAPACITY = 1600

#: Hard ceiling on the temporary share of the opening (v3 8).
DEFAULT_TEMPORARY_CAPACITY = 400

#: How long a task's lease runs from its last sign of activity (v3 7).
DEFAULT_TASK_LEASE_DAYS = 14

#: Similarity at which two pains count as the same hole (4.1).
DEFAULT_MATCH_THRESHOLD = 0.45

#: Similarity at which a row is worth showing to the reporter at all.
SHOW_THRESHOLD = 0.25

#: How long a trace lives (4.2).
DEFAULT_TRACE_TTL_DAYS = 30

#: Ceiling on a temporary context window (7).
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
