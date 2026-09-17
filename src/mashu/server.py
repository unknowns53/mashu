"""MCP server exposing Mashu's knowledge and task tools."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from uuid import UUID

from mashu import (
    bootstrap,
    db,
    memories,
    nominations,
    projects,
    routing,
    scopes,
    task_history,
    tasks,
    traces,
)
from mashu.errors import (
    DuplicateTaskError,
    MashuError,
    ProjectBudgetError,
    StaleStateError,
)

ACTOR_ENV_VAR = "MASHU_AGENT"
DEFAULT_ACTOR = "agent"


def actor() -> str:
    """Return the identity configured for this MCP process."""
    return os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


def _plain(value: Any) -> Any:
    """Turn database values into values accepted by the MCP JSON boundary."""
    if isinstance(value, (UUID, datetime, date)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _failure(error: MashuError) -> dict[str, Any]:
    answer: dict[str, Any] = {"ok": False, "error": str(error)}
    if isinstance(error, DuplicateTaskError):
        answer["candidates"] = error.candidates
    elif isinstance(error, StaleStateError):
        answer["current"] = error.current
    elif isinstance(error, ProjectBudgetError):
        answer["breakdown"] = error.breakdown
    return _plain(answer)


def _scope(cur: Any, name: str | None) -> tuple[UUID | None, str | None, bool]:
    """Resolve an explicit scope or the longest route for the current cwd."""
    if name is not None:
        row = scopes.require_scope(cur, name)
        return row["scope_id"], row["name"], False
    scope_id, routed = routing.resolve(cur, os.getcwd())
    if scope_id is None:
        return None, None, routed
    cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
    row = cur.fetchone()
    return scope_id, row["name"] if row else None, routed


def build_server() -> Any:
    """Build the fifteen-tool MCP server used by an agent session."""
    from mcp.server import MCPServer

    # MCP instructions provide calling guidance to connected clients.
    server = MCPServer(
        name="mashu",
        instructions=(
            "Mashu keeps only knowledge whose absence has provably cost "
            "something; everything merely useful was deliberately left out. "
            "Call session_bootstrap once, first, at the start of every "
            "session: knowledge is pushed, there is no search for it, and a "
            "session that skips the call works blind without knowing it. "
            "Then two habits while you work. trace_put one line for anything "
            "you had to look up or derive — a dated observation that lets a "
            "later repeat be proven. pain_report when missing or stale "
            "knowledge actually cost something: wrong work (incident) or a "
            "repeated lookup (friction); the second time is what turns a "
            "pain into a candidate. If the user explicitly says to remember "
            "something, carry it with memory_nominate — it waits for their "
            "confirmation. Temporary, expiring conditions are recorded by "
            "the user's own hand (mashu remember --until), not by agents. "
            "Nothing you write becomes knowledge without a human decision, "
            "and retired knowledge answers with the reason it was retired: "
            "bring new grounds rather than re-deriving it. Four work verbs "
            "keep Project State small: if you looked something up, trace_put; "
            "if it hurt, pain_report; if the user said remember, "
            "memory_nominate; at a break in the work, task_checkpoint. Use "
            "attempt_record for the outcome of a failed try and decision_record "
            "for a judgement whose reason would be costly to re-derive. A task "
            "state is what was last confirmed on its date, not current truth; "
            "when that date is old, verify it against the repository and its "
            "artifacts before working from it. Task completion is never the "
            "agent's call: closing is the user's, via the CLI. When work you "
            "finish looks like the end of its task, say so with "
            "task_propose_close — the outcome you would choose and the "
            "grounds for it — instead of writing 'this can be closed' into "
            "the state, where the user has to read it back out and retype it. "
            "The proposal decides nothing; it waits on the closing screen."
        ),
    )

    @server.tool()
    def session_bootstrap(scope: str | None = None) -> dict[str, Any]:
        """Return session memories, task state, and temporary context; call once at startup."""
        try:
            with db.transaction() as cur:
                scope_id, scope_name, routed = _scope(cur, scope)
                answer = bootstrap.session_bootstrap(
                    cur,
                    actor=actor(),
                    scope_id=scope_id,
                    scope_name=scope_name,
                    routed=routed,
                )
                note = (
                    "Knowledge is pushed, not searched. Call trace_put for a "
                    "later re-derivation and pain_report when forgetting caused pain; "
                    "nothing an agent writes becomes knowledge without a human decision."
                )
                if answer["schema_pending"]:
                    # Report schema status before sending usage guidance.
                    note = (
                        "This store is behind the code: "
                        f"{', '.join(answer['schema_pending'])} not applied, so any tool "
                        "writing the columns they add will fail. Ask the user to run "
                        "'mashu admin migrate'. " + note
                    )
                answer = {**answer, "ok": True, "note": note}
                return _plain(answer)
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def pain_report(
        kind: str,
        what: str,
        prevention: str,
        scope: str | None = None,
        prevention_kind: str = "rule",
        task_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Record a pain and show related evidence.

        Use `rule` for reusable guidance or `work` with `task_id` for a one-time
        fix; this never admits knowledge directly.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                from mashu import ledger

                answer = ledger.report_pain(
                    cur,
                    kind=kind,
                    what=what,
                    prevention=prevention,
                    actor=actor(),
                    scope_id=scope_id,
                    prevention_kind=prevention_kind,
                    task_id=task_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def trace_put(content: str, scope: str | None = None) -> dict[str, Any]:
        """Record a dated observation for later matching.

        Traces are not knowledge and are not pushed to sessions.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                answer = traces.put_trace(
                    cur,
                    content=content,
                    actor=actor(),
                    scope_id=scope_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def trace_search(
        query: str | None = None,
        scope: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Search dated, unverified traces; results are observations, not current knowledge."""
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                found = traces.search_traces(
                    cur,
                    query=query,
                    scope_id=scope_id,
                    limit=limit,
                )
                rows = [{**row, "created_at": row.get("created_at")} for row in found]
                return _plain(
                    {
                        "ok": True,
                        "traces": rows,
                        "note": "These are dated, unverified observations, not knowledge.",
                    }
                )
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_list(scope: str) -> dict[str, Any]:
        """List active knowledge for one named scope."""
        try:
            with db.transaction() as cur:
                row = scopes.require_scope(cur, scope)
                found = memories.active_memories(cur, scope_id=row["scope_id"])
                return _plain({"ok": True, "scope": row["name"], "memories": found})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_nominate(content: str, scope: str | None = None) -> dict[str, Any]:
        """Submit an agent-carried user instruction for human review.

        This does not activate the rule.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                answer = nominations.nominate_user_explicit(
                    cur,
                    content=content,
                    actor=actor(),
                    scope_id=scope_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def project_list(include_archived: bool = False) -> dict[str, Any]:
        """List projects and the task counts they currently carry."""
        try:
            with db.transaction() as cur:
                found = projects.list_projects(cur, include_archived=include_archived)
                return _plain({"ok": True, "projects": found})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_create(
        project: UUID | str,
        name: str,
        goal: str | None = None,
        approach: str | None = None,
        status_text: str | None = None,
        open_questions: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Create a task; likely duplicates are returned unless `force` is true."""
        try:
            with db.transaction() as cur:
                answer = tasks.task_create(
                    cur,
                    project=project,
                    name=name,
                    actor=actor(),
                    goal=goal,
                    approach=approach,
                    status_text=status_text,
                    open_questions=open_questions,
                    blockers=blockers,
                    next_actions=next_actions,
                    force=force,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_get(
        task_id: UUID,
        attempts: bool = False,
        decisions: bool = False,
        artifacts: bool = False,
        checkpoints: bool = False,
    ) -> dict[str, Any]:
        """Get a task and only the requested history expansions."""
        try:
            with db.transaction() as cur:
                answer = task_history.expanded_task(
                    cur,
                    task_id,
                    attempts=attempts,
                    decisions=decisions,
                    artifacts=artifacts,
                    checkpoints=checkpoints,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_search(
        query: str,
        project: UUID | str | None = None,
        include_closed: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search task names and current state, including closed tasks on request."""
        try:
            with db.transaction() as cur:
                found = tasks.task_search(
                    cur,
                    query,
                    project=project,
                    include_closed=include_closed,
                    limit=limit,
                )
                return _plain({"ok": True, "tasks": found})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_update(
        task_id: UUID,
        expect_updated_at: datetime,
        goal: str | None = None,
        approach: str | None = None,
        status_text: str | None = None,
        open_questions: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace full state; omitted fields clear.

        Pass the read state's `updated_at` as `expect_updated_at`.
        """
        try:
            with db.transaction() as cur:
                answer = tasks.task_update(
                    cur,
                    task_id,
                    actor=actor(),
                    expect_updated_at=expect_updated_at,
                    goal=goal,
                    approach=approach,
                    status_text=status_text,
                    open_questions=open_questions,
                    blockers=blockers,
                    next_actions=next_actions,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_checkpoint(
        task_id: UUID,
        what_changed: str,
        expect_updated_at: datetime,
        goal: str | None = None,
        approach: str | None = None,
        status_text: str | None = None,
        open_questions: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
        evidence: list[UUID] | None = None,
    ) -> dict[str, Any]:
        """Replace full state and record a checkpoint; omitted fields clear.

        Pass the read state's `updated_at` as `expect_updated_at`.
        """
        try:
            with db.transaction() as cur:
                answer = task_history.checkpoint(
                    cur,
                    task_id,
                    actor=actor(),
                    what_changed=what_changed,
                    expect_updated_at=expect_updated_at,
                    goal=goal,
                    approach=approach,
                    status_text=status_text,
                    open_questions=open_questions,
                    blockers=blockers,
                    next_actions=next_actions,
                    evidence=evidence,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_propose_close(
        task_id: UUID,
        outcome: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record a proposed outcome and reason without closing the task.

        A user decides with `mashu task close`.
        """
        try:
            with db.transaction() as cur:
                answer = tasks.propose_close(
                    cur, task_id, outcome=outcome, reason=reason, actor=actor()
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_withdraw_close_proposal(task_id: UUID) -> dict[str, Any]:
        """Remove a close proposal without changing task state."""
        try:
            with db.transaction() as cur:
                answer = tasks.withdraw_proposal(cur, task_id, actor=actor())
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def attempt_record(
        task_id: UUID,
        attempt: str,
        result: str | None = None,
        reason: str | None = None,
        next: str | None = None,
    ) -> dict[str, Any]:
        """Record the outcome and next step of a failed try."""
        try:
            with db.transaction() as cur:
                answer = task_history.attempt_record(
                    cur,
                    task_id,
                    actor=actor(),
                    attempt=attempt,
                    result=result,
                    reason=reason,
                    next=next,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def decision_record(
        task_id: UUID,
        decision: str,
        reason: str | None = None,
        supersedes_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Record a judgement whose reason would be costly to re-derive."""
        try:
            with db.transaction() as cur:
                answer = task_history.decision_record(
                    cur,
                    task_id,
                    actor=actor(),
                    decision=decision,
                    reason=reason,
                    supersedes_id=supersedes_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def artifact_link(
        task_id: UUID,
        kind: str,
        locator: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Link a task to the external source of an artifact."""
        try:
            with db.transaction() as cur:
                answer = task_history.artifact_link(
                    cur,
                    task_id,
                    actor=actor(),
                    kind=kind,
                    locator=locator,
                    label=label,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    return server


def main() -> None:
    """Run the MCP server over stdio."""
    build_server().run()
