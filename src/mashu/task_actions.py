"""Human task operations shared by the command line, terminal screens, and desktop app."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from mashu import references, routing, scopes, task_history, tasks
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


def keep_open(cur: psycopg.Cursor, task_id: UUID, *, actor: str) -> dict[str, Any]:
    """Turn a close proposal down: the work is still live, so its lease is renewed too."""
    tasks.withdraw_proposal(cur, task_id, actor=actor)
    return tasks.touch(cur, task_id, actor=actor)


def task_project_choices(cur: psycopg.Cursor, *, cwd: str | None) -> ProjectChoices:
    """The scope this directory is routed to, or every open scope as a fallback.

    Each choice carries `project_id`, the id a task is filed under, which is the scope's id.
    """
    scope_id, _ = routing.resolve(cur, cwd)
    # SELECT * rather than naming archived_at, so a store without that column still answers.
    cur.execute("SELECT * FROM scope ORDER BY name")
    open_scopes = [
        {"project_id": row["scope_id"], "name": row["name"]}
        for row in cur.fetchall()
        if not row.get("archived_at")
    ]
    routed = [row for row in open_scopes if row["project_id"] == scope_id]
    if routed:
        return ProjectChoices(routed, routed[0]["name"])
    return ProjectChoices(open_scopes, None)


def task_view(cur: psycopg.Cursor, activity: str) -> list[dict[str, Any]]:
    """One task view, each task with the history a person may read or search."""
    return [
        task_history.expanded_task(
            cur,
            row["task"]["task_id"],
            attempts=True,
            decisions=True,
            artifacts=True,
            checkpoints=True,
            withdrawn_rulings=True,
        )
        for row in tasks.task_list(cur, activity=activity)
    ]


def create_task(
    cur: psycopg.Cursor,
    *,
    name: str,
    project: str | None,
    goal: str | None,
    cwd: str | None,
    actor: str,
    force: bool = False,
) -> dict[str, Any]:
    """Open a task under the named project, or the only one available from cwd.

    Without force, a task that reads like one already open in the project raises
    DuplicateTaskError naming it, so the person can look before creating it anyway.
    """
    project_id = resolve_task_project(cur, project, cwd=cwd)
    return tasks.task_create(
        cur, project=project_id, name=name, goal=goal or None, actor=actor, force=force
    )


def resolve_task_project(cur: psycopg.Cursor, given: str | None, *, cwd: str | None) -> UUID:
    """The scope a task is filed under: the one named, or the one this directory is routed to."""
    if given:
        row = scopes.get_scope(cur, given)
        if row is not None and not row.get("archived_at"):
            return row["scope_id"]
        if references.is_uuid_ref(given):
            return references.resolve_id(
                cur, given, table="scope", column="scope_id", label="scope"
            )
        return scopes.require_scope(cur, given)["scope_id"]

    choices = task_project_choices(cur, cwd=cwd)
    if choices.scope_name:
        return choices.projects[0]["project_id"]
    named = ", ".join(row["name"] for row in choices.projects) or "none yet"
    raise MashuError(
        f"this folder belongs to no scope; name one with --project <name> ({named}), "
        "or add this folder to one"
    )
