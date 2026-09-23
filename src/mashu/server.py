"""MCP server exposing Mashu's knowledge and task tools."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from mashu import (
    bootstrap,
    db,
    memories,
    memory_changes,
    nominations,
    projects,
    routing,
    scopes,
    task_history,
    tasks,
    topics,
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

#: Claude Code truncates server instructions at 2 KB, so the most used guidance comes
#: first and per-tool procedure lives in each tool's description instead.
INSTRUCTIONS_BYTE_LIMIT = 2048
INSTRUCTIONS = (
    "Mashu pushes active Memory; call session_bootstrap first in every session. "
    "When the work at hand matches a bootstrap topic's trigger, call memory_list with that "
    "topic before starting it. "
    "Before continuing a task from a bootstrap card, call task_get with its full id and "
    "verify old state against the repository and artifacts. Use task_checkpoint at work "
    "breaks; put failed tries in its attempts, costly-to-rederive reasons in decisions, "
    "and external sources in artifacts. Task completion remains the user's decision. "
    "If work looks done, use task_propose_close with outcome and grounds; it changes no "
    "task state. "
    "Record lookups or derivations with trace_put. Use pain_report only when missing or "
    "stale knowledge caused an incident or repeated lookup; incidents nominate once, "
    "friction on the second occurrence. Use prevention_kind='work' and task_id for "
    "one-time fixes. Temporary Context handles expiry. "
    "Durable Memory changes need an explicit user instruction. For a request to remember, "
    "call memory_admit with the content. For an existing Memory, call memory_get, "
    "memory_change_propose, then memory_change_apply. Follow each tool's description for "
    "the required fields. Your own suggestion, confidence, and user silence are not "
    "approval; your independent change idea stays pending. After success, briefly report "
    "the target and reason; if it remains pending, say that review is needed."
)


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
    """Build the MCP server used by an agent session."""
    from mcp.server import MCPServer

    server = MCPServer(name="mashu", instructions=INSTRUCTIONS)
    # Each client session runs its own stdio server process, so one id per build names it.
    session = uuid4()

    @server.tool()
    def session_bootstrap(scope: str | None = None) -> dict[str, Any]:
        """Return session memories, the topic index, task cards, and temporary context."""
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
                    "only an explicit user instruction can authorize memory_admit or "
                    "memory_change_apply in the same session. The Agent executes it, and "
                    "the stored instruction quote is provenance rather than authentication. "
                    + bootstrap.TASK_DETAIL_INSTRUCTION
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
                    session=session,
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
    def memory_list(scope: str | None = None, topic: str | None = None) -> dict[str, Any]:
        """List active knowledge for one named scope, or read one topic's rules.

        Pass exactly one. With `topic`, read the rules before starting the work its
        trigger names; bootstrap lists topics but never their rules.
        """
        if (scope is None) == (topic is None):
            return _failure(MashuError("pass exactly one of scope or topic"))
        try:
            with db.transaction() as cur:
                if topic is not None:
                    answer = topics.read_for_session(cur, topic, actor=actor(), session=session)
                    return _plain({"ok": True, **answer})
                row = scopes.require_scope(cur, scope)
                found = memories.active_memories(cur, scope_id=row["scope_id"])
                return _plain({"ok": True, "scope": row["name"], "memories": found})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_nominate(content: str, scope: str | None = None) -> dict[str, Any]:
        """Create or update a pending candidate for a new durable Memory without admitting it.

        Use it for the successor of a memory_change_propose replace, or to read the current
        candidate again when memory_admit stopped at the queue. A plain explicit request to
        remember goes to memory_admit with `content` instead.
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
    def memory_get(memory_id: UUID) -> dict[str, Any]:
        """Read one Memory body, revision, evidence, and its complete retirement record."""
        try:
            with db.transaction() as cur:
                row = memories.memory_details(cur, memory_id)
                if row is None:
                    raise MashuError(f"no memory {memory_id}")
                return _plain({"ok": True, "memory": row})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_admit(
        request_id: UUID,
        approval_kind: str,
        instruction: str,
        nomination_id: UUID | None = None,
        nomination_version: int | None = None,
        content: str | None = None,
        conversation_ref: str | None = None,
        conflict_ids: list[UUID] | None = None,
        conflict_instruction: str | None = None,
        delivery: str | None = None,
        scope: str | None = None,
        guard_action: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """Admit a new Memory with explicit instruction provenance.

        For a plain request to remember, pass `content` (and optional `scope`): the
        nomination and admission happen in one call. If it joins an already pending
        candidate or repeats invalidated or legacy retired Memory, it stops with
        `admitted: false` and returns the nomination to read; then pass that
        `nomination_id` and its `version` as `nomination_version`. Always pass
        `approval_kind='user_instruction'`, a short exact quote of the user's instruction,
        any available `conversation_ref`, and a new `request_id`. The Agent executes it;
        this provenance is not authentication. Invalidated or legacy conflicts need
        `conflict_ids` and a `conflict_instruction` in which the user addresses them.
        For a rule the user wants read only during one kind of work, pass
        `delivery='topic'` and an existing `topic` name.
        """
        if approval_kind != "user_instruction":
            return _failure(MashuError("MCP admission requires approval_kind='user_instruction'"))
        if (nomination_id is None) == (content is None):
            return _failure(
                MashuError("pass exactly one of nomination_id (with nomination_version) or content")
            )
        if nomination_id is not None and nomination_version is None:
            return _failure(MashuError("nomination_id needs the nomination_version you read"))
        if content is not None and nomination_version is not None:
            return _failure(
                MashuError("nomination_version belongs with nomination_id, not with content")
            )
        approval = {
            "kind": approval_kind,
            "instruction": instruction,
            "conversation_ref": conversation_ref,
            "conflict_ids": conflict_ids or [],
            "conflict_instruction": conflict_instruction,
        }
        try:
            with db.transaction() as cur:
                topic_id = topics.require_topic(cur, topic)["topic_id"] if topic else None
                if content is not None:
                    scope_id, _, _ = _scope(cur, scope)
                    answer = nominations.remember_explicit(
                        cur,
                        content=content,
                        actor=actor(),
                        approval=approval,
                        request_id=request_id,
                        scope_id=scope_id,
                        delivery=delivery,
                        guard_action=guard_action,
                        topic_id=topic_id,
                    )
                    return _plain({"ok": answer["admitted"], **answer})
                cur.execute("SELECT * FROM nomination WHERE nomination_id = %s", (nomination_id,))
                nomination = cur.fetchone()
                if nomination is None:
                    raise MashuError(f"no nomination {nomination_id}")
                scope_id = (
                    scopes.require_scope(cur, scope)["scope_id"]
                    if scope
                    else nomination["scope_id"]
                )
                chosen_delivery = delivery or (
                    "topic" if topic_id else "scope" if scope_id else "always"
                )
                result = nominations.admit(
                    cur,
                    nomination_id,
                    actor=actor(),
                    delivery=chosen_delivery,
                    expected_version=nomination_version,
                    scope_id=scope_id,
                    scope_override=True,
                    guard_action=guard_action,
                    topic_id=topic_id,
                    approval=approval,
                    request_id=request_id,
                )
                return _plain({"ok": True, "memory": result})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_change_propose(
        target_memory_id: UUID,
        target_revision_id: UUID,
        target_updated_at: datetime,
        operation: str,
        evidence: list[dict[str, Any]],
        change_id: UUID | None = None,
        retirement_kind: str | None = None,
        retire_reason: str | None = None,
        successor_nomination_id: UUID | None = None,
        successor_nomination_version: int | None = None,
        successor_settings: dict[str, Any] | None = None,
        relocated_to_kind: str | None = None,
        relocated_to_id: UUID | None = None,
        restore_reason: str | None = None,
    ) -> dict[str, Any]:
        """Create or refresh a retire, replace, restore, or redeliver proposal.

        Call memory_get first. For replace, pass the successor nomination version just
        read; if it changed, reread it and update the proposal. Old delivery settings carry
        over by default; pass a complete `successor_settings` object with `delivery`,
        `scope_id`, and `guard_action` only when the requested change includes new
        delivery settings. `redeliver` changes only those settings of an active Memory
        and requires `successor_settings`; its evidence observations say why. For
        `delivery='topic'` add `topic`: an existing topic takes that topic's scope_id or
        null, and a new topic also needs `topic_trigger` (one sentence saying when to read
        it) with `scope_id` naming where it is listed, or null for every session.
        """
        try:
            with db.transaction() as cur:
                row = memory_changes.propose(
                    cur,
                    target_memory_id=target_memory_id,
                    target_revision_id=target_revision_id,
                    target_updated_at=target_updated_at,
                    operation=operation,
                    evidence=evidence,
                    actor=actor(),
                    retirement_kind=retirement_kind,
                    retire_reason=retire_reason,
                    successor_nomination_id=successor_nomination_id,
                    successor_nomination_version=successor_nomination_version,
                    successor_settings=successor_settings,
                    relocated_to_kind=relocated_to_kind,
                    relocated_to_id=relocated_to_id,
                    restore_reason=restore_reason,
                    change_id=change_id,
                )
                return _plain({"ok": True, "change": row})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_change_apply(
        change_id: UUID,
        version: int,
        request_id: UUID,
        approval_kind: str,
        instruction: str,
        conversation_ref: str | None = None,
        conflict_ids: list[UUID] | None = None,
        conflict_instruction: str | None = None,
        reversal_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Apply one exact proposal version with explicit user instruction provenance.

        Apply only the exact change the user explicitly requested, with
        `approval_kind='user_instruction'`, a short exact quote of the instruction, and
        `request_id`. If the successor changed and no longer matches that instruction, get
        a new instruction first. Invalidated or legacy conflicts need an instruction
        addressing those conflicts. This provenance is not authentication.
        """
        if approval_kind != "user_instruction":
            return _failure(MashuError("MCP apply requires approval_kind='user_instruction'"))
        try:
            with db.transaction() as cur:
                result = memory_changes.apply(
                    cur,
                    change_id,
                    version=version,
                    request_id=request_id,
                    approval={
                        "kind": approval_kind,
                        "instruction": instruction,
                        "conversation_ref": conversation_ref,
                        "conflict_ids": conflict_ids or [],
                        "conflict_instruction": conflict_instruction,
                        "reversal_instruction": reversal_instruction,
                    },
                    actor=actor(),
                )
                return _plain({"ok": True, **result})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_change_withdraw(change_id: UUID, reason: str) -> dict[str, Any]:
        """Withdraw an unneeded pending Memory change proposal."""
        try:
            with db.transaction() as cur:
                row = memory_changes.decide(
                    cur, change_id, status="withdrawn", actor=actor(), reason=reason
                )
                return _plain({"ok": True, "change": row})
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
        """Get full current state and requested history; call before working on a task."""
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
        """Replace the state fields given; omitted fields keep their value, "" or [] clears.

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
        attempts: list[dict[str, Any]] | None = None,
        decisions: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Replace the state fields given and record a checkpoint with its history.

        Omitted fields keep their value; "" or [] clears one. Pass the read state's
        `updated_at` as `expect_updated_at`. Optional history, all
        written with the checkpoint or not at all:
        `attempts` items `{attempt, result?, reason?, next?}` for failed tries,
        `decisions` items `{decision, reason?, supersedes_id?}` for reasons costly to
        re-derive, and `artifacts` items `{kind, locator, label?}` for external sources.
        Artifact kinds: git_commit, git_branch, file, document, obsidian, issue, dataset,
        log, url, other. Artifacts linked here join `evidence` automatically.
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
                    attempts=attempts,
                    decisions=decisions,
                    artifacts=artifacts,
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

    return server


def main() -> None:
    """Run the MCP server over stdio."""
    build_server().run()
