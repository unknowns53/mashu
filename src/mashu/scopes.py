"""When a scope is ready to answer (specification 7.1).

The migration of 27.1 puts a scope into a state the rest of the design does
not otherwise produce: every memory in it is a candidate and none is adopted.
Retrieval answers that scope with nothing, correctly, because nothing in it is
settled. What is wrong is not the answer but that an agent reading it cannot
tell "there is nothing here" from "this is not open yet", and the second is
the one that should make it wait rather than go and derive its own.

Readiness is a manifest rather than a count. Section 27.1 says a scope has
migrated once its current state, its active preferences and its main decisions
stand up, and read as three required items that becomes a rule that makes a
scope with no decisions in it invent one. So each of the three is either
required or declared unnecessary, and the declaration is the user's, like the
scope itself (7).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu.errors import MashuError, NotFoundError
from mashu.events import record
from mashu.models import EventType, Lifecycle, MemoryType

#: The three section 27.1 names. Nothing else gates promotion: the rest of a
#: scope's contents are what it accumulates, not what it needs to open.
READINESS_TYPES = (MemoryType.STATE, MemoryType.PREFERENCE, MemoryType.DECISION)

REQUIRED = "required"
NOT_NEEDED = "not_needed"


class NotReadyError(MashuError):
    """A scope was asked to open before what it declared it needed stands up."""

    def __init__(self, message: str, missing: list[str]) -> None:
        super().__init__(message)
        self.missing = missing


_STANDING_SQL = """
SELECT s.scope_id, s.name, s.lifecycle, s.readiness,
       count(*) FILTER (WHERE e.active_version IS NOT NULL AND e.type = 'state')      AS state,
       count(*) FILTER (WHERE e.active_version IS NOT NULL AND e.type = 'preference') AS preference,
       count(*) FILTER (WHERE e.active_version IS NOT NULL AND e.type = 'decision')   AS decision,
       count(*) FILTER (WHERE e.active_version IS NOT NULL)                           AS adopted,
       count(*) FILTER (WHERE pending.version_id IS NOT NULL)                         AS pending
FROM scope s
LEFT JOIN memory_entity e ON e.scope_id = s.scope_id
LEFT JOIN LATERAL (
    SELECT v.version_id FROM memory_version v
    WHERE v.memory_id = e.memory_id AND v.status = 'candidate'
      AND (e.active_version IS NULL OR e.active_version <> v.version_id)
    LIMIT 1
) pending ON true
WHERE s.status = 'active' AND (%(scope)s::uuid IS NULL OR s.scope_id = %(scope)s)
GROUP BY s.scope_id, s.name, s.lifecycle, s.readiness
ORDER BY s.name
"""


def readiness(cur: psycopg.Cursor, scope_id: UUID | None = None) -> list[dict[str, Any]]:
    """Every active scope, what it declared it needs, and what stands up."""
    cur.execute(_STANDING_SQL, {"scope": scope_id})
    out = []
    for row in cur.fetchall():
        manifest = row["readiness"] or {}
        missing = [
            str(type_)
            for type_ in READINESS_TYPES
            if manifest.get(str(type_), REQUIRED) == REQUIRED and row[str(type_)] == 0
        ]
        out.append(
            {
                "scope_id": row["scope_id"],
                "name": row["name"],
                "lifecycle": Lifecycle(row["lifecycle"]),
                "manifest": {str(t): manifest.get(str(t), REQUIRED) for t in READINESS_TYPES},
                "standing": {str(t): row[str(t)] for t in READINESS_TYPES},
                "adopted": row["adopted"],
                "pending": row["pending"],
                "missing": missing,
                "ready": not missing,
            }
        )
    return out


def set_requirement(
    cur: psycopg.Cursor,
    *,
    scope_id: UUID,
    type: MemoryType,
    requirement: str,
    actor: str,
) -> dict[str, Any]:
    """Declare one type required, or declare it unnecessary for this scope."""
    type = MemoryType(type)
    if type not in READINESS_TYPES:
        allowed = ", ".join(str(t) for t in READINESS_TYPES)
        raise MashuError(f"{type} is not part of readiness; only {allowed} are")
    if requirement not in (REQUIRED, NOT_NEEDED):
        raise MashuError(f"a requirement is {REQUIRED} or {NOT_NEEDED}, not {requirement!r}")

    cur.execute(
        "UPDATE scope SET readiness = readiness || %s::jsonb "
        "WHERE scope_id = %s RETURNING readiness",
        (f'{{"{type}": "{requirement}"}}', scope_id),
    )
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no scope {scope_id}")
    record(
        cur,
        EventType.SCOPE_READINESS_SET,
        actor,
        detail={"scope_id": str(scope_id), "type": str(type), "requirement": requirement},
    )
    return row["readiness"]


def promote(cur: psycopg.Cursor, *, scope_id: UUID, actor: str) -> dict[str, Any]:
    """Open a scope for use, refusing while what it declared it needs is absent.

    Promotion is not a formality. It is the point at which the scope stops
    being a pile of unreviewed imports and starts being something an agent is
    told it can rely on, and the only evidence for that is that a person has
    adopted the things the scope said it could not open without.
    """
    state = readiness(cur, scope_id)
    if not state:
        raise NotFoundError(f"no active scope {scope_id}")
    scope = state[0]
    if scope["lifecycle"] is Lifecycle.OPERATIONAL:
        return scope
    if scope["missing"]:
        raise NotReadyError(
            f"{scope['name']} still needs an adopted {', '.join(scope['missing'])}; "
            f"review one, or declare the type unnecessary for this scope",
            scope["missing"],
        )

    cur.execute(
        "UPDATE scope SET lifecycle = %s WHERE scope_id = %s",
        (str(Lifecycle.OPERATIONAL), scope_id),
    )
    record(
        cur,
        EventType.SCOPE_PROMOTED,
        actor,
        detail={"scope_id": str(scope_id), "name": scope["name"], "adopted": scope["adopted"]},
    )
    return readiness(cur, scope_id)[0]
