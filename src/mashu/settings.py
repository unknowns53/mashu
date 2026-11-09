"""Values the user sets from the Settings screen, read from the store at each use."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from mashu import config, events
from mashu.errors import MashuError

#: Longest interval the setting accepts; a day already stops a session writing twice.
MAX_CHECKPOINT_INTERVAL_MINUTES = 1440


def checkpoint_interval_minutes(cur: psycopg.Cursor) -> int:
    """How long one session waits between two writes of a task's state; zero for no wait."""
    # A store that has not taken the migration yet runs on the default.
    cur.execute("SELECT to_regclass('setting') IS NOT NULL AS present")
    if not cur.fetchone()["present"]:
        return config.DEFAULT_CHECKPOINT_INTERVAL_MINUTES
    cur.execute("SELECT value FROM setting WHERE name = 'checkpoint_interval_minutes'")
    row = cur.fetchone()
    return config.DEFAULT_CHECKPOINT_INTERVAL_MINUTES if row is None else row["value"]


def set_checkpoint_interval_minutes(cur: psycopg.Cursor, minutes: int, *, actor: str) -> int:
    if not 0 <= minutes <= MAX_CHECKPOINT_INTERVAL_MINUTES:
        raise MashuError(
            f"the checkpoint interval is 0 to {MAX_CHECKPOINT_INTERVAL_MINUTES} minutes, "
            f"not {minutes}"
        )
    before = checkpoint_interval_minutes(cur)
    cur.execute(
        """
        INSERT INTO setting (name, value, updated_by)
        VALUES ('checkpoint_interval_minutes', %s, %s)
        ON CONFLICT (name) DO UPDATE
        SET value = EXCLUDED.value, updated_at = clock_timestamp(),
            updated_by = EXCLUDED.updated_by
        """,
        (minutes, actor),
    )
    events.record(
        cur,
        "setting_changed",
        actor,
        detail={"name": "checkpoint_interval_minutes", "from": before, "to": minutes},
    )
    return minutes


@dataclass(frozen=True)
class Limit:
    default: Callable[[], int]
    low: int
    high: int
    unit: str


_TOKENS = (50, 20000, "tokens")

#: Every delivery limit a person may set. The ranges are the setting table's CHECK.
LIMITS: dict[str, Limit] = {
    "total_capacity": Limit(config.total_capacity, *_TOKENS),
    "memory_capacity": Limit(config.capacity, *_TOKENS),
    "always_capacity": Limit(config.always_capacity, *_TOKENS),
    "topic_capacity": Limit(config.topic_capacity, *_TOKENS),
    "project_capacity": Limit(config.project_capacity, *_TOKENS),
    "card_overflow_capacity": Limit(config.card_overflow_capacity, *_TOKENS),
    "task_card_capacity": Limit(config.task_card_capacity, *_TOKENS),
    "task_detail_capacity": Limit(config.task_detail_capacity, *_TOKENS),
    "decision_capacity": Limit(config.decision_capacity, *_TOKENS),
    "temporary_capacity": Limit(config.temporary_capacity, *_TOKENS),
    "content_limit": Limit(config.content_limit, 20, 1000, "chars"),
}

#: (share, whole): the share is delivered inside the whole, so it may not exceed it.
NESTED = (
    ("always_capacity", "memory_capacity"),
    ("memory_capacity", "total_capacity"),
    ("project_capacity", "total_capacity"),
    ("temporary_capacity", "total_capacity"),
    ("task_card_capacity", "project_capacity"),
    ("task_card_capacity", "task_detail_capacity"),
)


def _stored(cur: psycopg.Cursor) -> dict[str, int]:
    # A store that has not taken the migration yet runs on the defaults.
    cur.execute("SELECT to_regclass('setting') IS NOT NULL AS present")
    if not cur.fetchone()["present"]:
        return {}
    cur.execute("SELECT name, value FROM setting WHERE name = ANY(%s)", (list(LIMITS),))
    return {row["name"]: row["value"] for row in cur.fetchall()}


def limits(cur: psycopg.Cursor) -> dict[str, int]:
    """Every delivery limit in force: the one set on the Settings screen, else the default."""
    stored = _stored(cur)
    return {name: stored.get(name, limit.default()) for name, limit in LIMITS.items()}


def limit(cur: psycopg.Cursor, name: str) -> int:
    return limits(cur)[name]


def limit_rows(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Each limit with its value, default, range, and whether a person set it."""
    stored = _stored(cur)
    return [
        {
            "name": name,
            "value": stored.get(name, limit.default()),
            "default": limit.default(),
            "set": name in stored,
            "low": limit.low,
            "high": limit.high,
            "unit": limit.unit,
        }
        for name, limit in LIMITS.items()
    ]


def set_limits(
    cur: psycopg.Cursor, changes: dict[str, int | None], *, actor: str
) -> dict[str, int]:
    """Set several limits at once; None returns one to its default.

    They are checked together, so a share and the whole it sits in can move in one save.
    A limit below what is delivered now is accepted: writes that add to that share are then
    refused, and writes that shrink it still pass (capacity._judge).
    """
    unknown = sorted(set(changes) - set(LIMITS))
    if unknown:
        raise MashuError(f"no such limit: {', '.join(unknown)}")
    problems = [
        f"{name} is {LIMITS[name].low} to {LIMITS[name].high} {LIMITS[name].unit}, not {value}"
        for name, value in changes.items()
        if value is not None and not LIMITS[name].low <= value <= LIMITS[name].high
    ]
    before = limits(cur)
    after = {
        **before,
        **{
            name: LIMITS[name].default() if value is None else value
            for name, value in changes.items()
        },
    }
    problems += [
        f"{share} ({after[share]}) may not exceed {whole} ({after[whole]}), which delivers it"
        for share, whole in NESTED
        if after[share] > after[whole]
    ]
    if problems:
        raise MashuError("; ".join(problems))
    for name, value in changes.items():
        if value is None:
            cur.execute("DELETE FROM setting WHERE name = %s", (name,))
        else:
            cur.execute(
                """
                INSERT INTO setting (name, value, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                SET value = EXCLUDED.value, updated_at = clock_timestamp(),
                    updated_by = EXCLUDED.updated_by
                """,
                (name, value, actor),
            )
        if before[name] != after[name]:
            events.record(
                cur,
                "setting_changed",
                actor,
                detail={"name": name, "from": before[name], "to": after[name]},
            )
    return after
