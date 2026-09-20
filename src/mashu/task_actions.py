"""Human task operations shared by the command line and terminal screens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from mashu import projects, references, routing, tasks
from mashu.errors import MashuError

_PROPOSAL_FIELDS = ("outcome", "reason", "state_at", "proposed_at", "proposed_by")


@dataclass(frozen=True)
class ProjectChoices:
    projects: list[dict[str, Any]]
    scope_name: str | None


def close_task(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    outcome: str,
    reason: str | None,
    actor: str,
) -> dict[str, Any]:
    """Close on the outcome and reason the person supplied."""
    return tasks.close(cur, task_id, outcome=outcome, reason=reason, actor=actor)


def accept_close_proposal(
    cur: psycopg.Cursor,
    task_id: UUID,
    *,
    expected_proposal: dict[str, Any],
    expected_state: datetime,
    actor: str,
) -> dict[str, Any]:
    """Accept exactly the proposal and task state shown to the person."""
    current = tasks.lock_close_snapshot(cur, task_id)
    proposal = current.get("proposal")
    if (
        current["state"]["updated_at"] != expected_state
        or proposal is None
        or any(proposal[field] != expected_proposal.get(field) for field in _PROPOSAL_FIELDS)
    ):
        raise MashuError("the task or close proposal changed; review the current version first")
    return tasks.close(
        cur,
        task_id,
        outcome=proposal["outcome"],
        reason=proposal["reason"],
        actor=actor,
    )


def task_project_choices(cur: psycopg.Cursor, *, cwd: str | None) -> ProjectChoices:
    """List open projects in the routed scope, or all open projects as a fallback."""
    scope_id, _ = routing.resolve(cur, cwd)
    scope_name = None
    if scope_id is not None:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
        scope = cur.fetchone()
        scope_name = scope["name"] if scope else None
        cur.execute(
            "SELECT project_id, name FROM project "
            "WHERE scope_id = %s AND archived_at IS NULL ORDER BY name",
            (scope_id,),
        )
        scoped = cur.fetchall()
        if scoped:
            return ProjectChoices(scoped, scope_name)
    cur.execute("SELECT project_id, name FROM project WHERE archived_at IS NULL ORDER BY name")
    return ProjectChoices(cur.fetchall(), None)


def resolve_task_project(cur: psycopg.Cursor, given: str | None, *, cwd: str | None) -> UUID:
    """Resolve a named project or the only project available from this directory."""
    if given:
        row = projects.get_project(cur, given)
        if row is not None:
            return row["project_id"]
        if references.is_uuid_ref(given):
            return references.resolve_id(
                cur,
                given,
                table="project",
                column="project_id",
                label="project",
            )
        return projects.require_project(cur, given)["project_id"]

    choices = task_project_choices(cur, cwd=cwd)
    if len(choices.projects) == 1:
        return choices.projects[0]["project_id"]
    if len(choices.projects) > 1:
        named = ", ".join(row["name"] for row in choices.projects)
        if choices.scope_name:
            raise MashuError(
                f"scope '{choices.scope_name}' holds more than one project ({named}): "
                "choose one with --project <name>"
            )
        raise MashuError(
            f"mashu task create needs --project <name>; available projects: {named}. "
            "Example: mashu task create <name> --project <project>"
        )
    raise MashuError(
        "there are no open projects. Create one with 'mashu project create <name>', "
        "then assign the task with 'mashu task create <task> --project <project>'"
    )
