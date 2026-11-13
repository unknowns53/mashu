"""Read Mashu configuration from the environment.

The limits here are defaults; a value set on the Settings screen overrides them (settings.py).
"""

from __future__ import annotations

import os
import pathlib

#: Overall limit for memory, project-state, and temporary-context payloads (project-state 8).
DEFAULT_TOTAL_CAPACITY = 4000

#: Hard ceiling on the memories alone, in estimated tokens (5.2).
DEFAULT_CAPACITY = 2000

#: Hard ceiling on the always layer alone, inside the whole-opening one above (5.2).
DEFAULT_ALWAYS_CAPACITY = 800

#: Task cards delivered to one session, newest activity first, in estimated tokens
#: (project-state 8).
#: Agents fill whatever share they are given, so the cut is made at delivery, not at a write.
DEFAULT_PROJECT_CAPACITY = 800

#: The line naming active tasks that lost their card, in estimated tokens (project-state 8).
DEFAULT_CARD_OVERFLOW_CAPACITY = 200

#: Maximum pushed index card for one active task.
DEFAULT_TASK_CARD_CAPACITY = 200

#: Maximum full current state retrieved for one task.
DEFAULT_TASK_DETAIL_CAPACITY = 800

#: How many of the user's rulings one task holds; task_get returns them all (project-state 5.4).
DEFAULT_RULING_COUNT = 10

#: Characters one ruling may hold. The user's words beside it are held to their own ceiling.
DEFAULT_RULING_CHARS = 120

#: Characters of the user's own words a ruling or a withdrawal quotes.
DEFAULT_RULING_WORDS_CHARS = 300

#: Hard ceiling on the temporary share of the opening (project-state 8).
DEFAULT_TEMPORARY_CAPACITY = 400

#: Characters one Memory body may hold; its reason goes to `why` (5.2).
DEFAULT_CONTENT_LIMIT = 120

#: Maximum rule bodies one topic holds; they are read on demand, not pushed (5.2).
DEFAULT_TOPIC_CAPACITY = 800

#: How long a task's lease runs from its last sign of activity (project-state 7).
DEFAULT_TASK_LEASE_DAYS = 14

#: How long one session waits between two writes of a task's state until the user sets
#: another value on the Settings screen (settings.py).
DEFAULT_CHECKPOINT_INTERVAL_MINUTES = 30

#: Similarity at which two pains count as the same hole (4.1).
DEFAULT_MATCH_THRESHOLD = 0.45

#: Similarity at which a row is worth showing to the reporter at all.
SHOW_THRESHOLD = 0.25

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


def card_overflow_capacity() -> int:
    return int(os.environ.get("MASHU_CARD_OVERFLOW_CAPACITY") or DEFAULT_CARD_OVERFLOW_CAPACITY)


def ruling_count() -> int:
    return int(os.environ.get("MASHU_RULING_COUNT") or DEFAULT_RULING_COUNT)


def ruling_chars() -> int:
    return int(os.environ.get("MASHU_RULING_CHARS") or DEFAULT_RULING_CHARS)


def ruling_words_chars() -> int:
    return int(os.environ.get("MASHU_RULING_WORDS_CHARS") or DEFAULT_RULING_WORDS_CHARS)


def task_card_capacity() -> int:
    return int(os.environ.get("MASHU_TASK_CARD_CAPACITY") or DEFAULT_TASK_CARD_CAPACITY)


def task_detail_capacity() -> int:
    return int(os.environ.get("MASHU_TASK_DETAIL_CAPACITY") or DEFAULT_TASK_DETAIL_CAPACITY)


def temporary_capacity() -> int:
    return int(os.environ.get("MASHU_TEMPORARY_CAPACITY") or DEFAULT_TEMPORARY_CAPACITY)


def content_limit() -> int:
    return int(os.environ.get("MASHU_CONTENT_LIMIT") or DEFAULT_CONTENT_LIMIT)


def topic_capacity() -> int:
    return int(os.environ.get("MASHU_TOPIC_CAPACITY") or DEFAULT_TOPIC_CAPACITY)


def task_lease_days() -> int:
    return int(os.environ.get("MASHU_TASK_LEASE_DAYS") or DEFAULT_TASK_LEASE_DAYS)


def match_threshold() -> float:
    return float(os.environ.get("MASHU_MATCH_THRESHOLD") or DEFAULT_MATCH_THRESHOLD)


def vault_root() -> pathlib.Path | None:
    """The Obsidian vault topic sources are relative to, or None when none is configured.

    It comes only from the environment, because the absolute path names the user.
    """
    value = os.environ.get("MASHU_VAULT_ROOT")
    return pathlib.Path(value).expanduser() if value else None
