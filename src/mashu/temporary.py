"""Conditions that stop applying on their own (specification 7).

The one part of v1 kept unchanged, because it never had the problem the rest
had. A temporary context knows its own expiry at the moment it is written, so
it needs no review to enter and no upkeep to leave: this week's maintenance
window, tomorrow's reset quota. Nobody has to remember to withdraw it.

The fourteen-day ceiling is the whole of the discipline. A claim that wants
longer is not temporary; it is a permanent claim wearing an expiry date so it
can skip the evidence it would otherwise need, and the refusal says where it
belongs instead.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import capacity, config, events, redact
from mashu.errors import RefusedError

_TOO_LONG = (
    "a temporary context lasts at most {max} days; a claim that wants {asked} is an "
    "indefinite claim wearing an expiry. If forgetting it has cost something, report "
    "that with pain_report; if it is simply true, record it with mashu remember."
)


def put_temporary(
    cur: psycopg.Cursor,
    *,
    content: str,
    actor: str,
    days: float,
    scope_id: UUID | None = None,
) -> dict[str, Any]:
    """Record a condition together with the moment it stops being one."""
    if not 0 < days <= config.TEMPORARY_MAX_DAYS:
        raise RefusedError(_TOO_LONG.format(max=config.TEMPORARY_MAX_DAYS, asked=f"{days:g} days"))
    verdict = redact.check(content)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())

    # A temporary context is pushed, so it takes a seat while it lasts (5.2).
    # Expiring on its own is why it needs no review; it is not a reason to be
    # weightless in an opening it is being carried in. An unscoped one reaches
    # every session, which is what 'always' means here.
    admission = capacity.check_admission(
        cur,
        content=content,
        delivery="always" if scope_id is None else "scope",
        scope_id=scope_id,
    )
    if not admission["ok"]:
        raise RefusedError(admission["refusal"])

    cur.execute(
        """
        INSERT INTO temporary_context (content, scope_id, created_by, expires_at)
        VALUES (%s, %s, %s, now() + make_interval(secs => %s))
        RETURNING *
        """,
        (content, scope_id, actor, float(days) * 86400.0),
    )
    row = cur.fetchone()
    events.record(cur, "temporary_recorded", actor, detail={"days": days})
    report: dict[str, Any] = {**row, "unchecked": verdict.unchecked}
    if verdict.malformed:
        report["malformed"] = verdict.malformed
    return report


def active_temporary(cur: psycopg.Cursor, *, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """What still applies here. Expired rows are filtered, never deleted."""
    cur.execute(
        """
        SELECT * FROM temporary_context
        WHERE expires_at > now()
          AND (scope_id IS NULL OR scope_id = %(scope)s::uuid)
        ORDER BY expires_at
        """,
        {"scope": scope_id},
    )
    return cur.fetchall()
