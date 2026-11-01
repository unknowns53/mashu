"""Values the user sets from the Settings screen, read from the store at each use."""

from __future__ import annotations

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
