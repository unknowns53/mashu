"""MCP server exposing Mashu's knowledge and task tools."""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from typing import Annotated, Any, NotRequired
from uuid import UUID, uuid4

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    # pydantic, which builds the tool schemas, refuses typing.TypedDict before 3.12.
    from typing_extensions import TypedDict

from mashu import (
    approvals,
    bootstrap,
    capacity,
    config,
    db,
    events,
    memories,
    memory_changes,
    nominations,
    projects,
    references,
    routing,
    scopes,
    task_history,
    tasks,
    topics,
)
from mashu.errors import (
    DuplicateTaskError,
    MalformedRequestError,
    MashuError,
    OverLimitError,
    RefusedError,
    RetiredConflictError,
    StaleStateError,
)

ACTOR_ENV_VAR = "MASHU_AGENT"
DEFAULT_ACTOR = "agent"

#: Claude Code truncates server instructions at 2 KB, so the most used guidance comes
#: first and per-tool procedure lives in each tool's description instead.
INSTRUCTIONS_BYTE_LIMIT = 2048
INSTRUCTIONS = (
    "Call session_bootstrap first unless a SessionStart hook already delivered Mashu in this "
    "session. When the work at hand matches a bootstrap topic's trigger, call memory_list "
    "with that topic before starting it. "
    "Memory holds standing rules only, pushed to every session (always), to one place "
    "(scope), or when one kind of work starts (topic). Project facts, findings, and settled "
    "decisions go in a task's decisions, which only task_get reads; notes go in project "
    "docs. "
    "Before continuing a task from a card, call task_get with its full id and check old "
    "state against the repository. task_checkpoint is the one task state write: call it "
    "when a try fails, a costly decision is made, or the approach or next actions change, "
    "and before you stop; not after each commit. Put failed tries in attempts, reasons in "
    "decisions, external sources in artifacts. Open a task only for work that spans "
    "sessions. Completion is the user's call: after your last checkpoint, "
    "task_propose_close may state the outcome. "
    "pain_report is only for wrong work that missing or stale knowledge caused. If the "
    "knowledge was there, name where (violated_memory_id, ignored_task_id, ignored_source) "
    "and no candidate is made; a one-time fix, such as editing a doc, is "
    "prevention_kind='work' with task_id. "
    "Durable Memory changes need the user's explicit instruction. Asked to remember a "
    "standing rule, call memory_admit; 'remember' can also mean take it to heart, so ask "
    "before writing when the user named neither Mashu nor a lasting rule. Retire, replace, "
    "or restore as asked takes memory_change_instructed, a delivery move memory_redeliver; "
    "a standing proposal they approve memory_change_apply; your own idea goes to "
    "memory_change_propose and stays pending, and your confidence or their silence is not "
    "approval. Temporary Context and "
    "the review queue are the user's. "
    "Writes naming people or home paths are refused: call the user 'the user' and write "
    "paths from ~. After a write, report its target and reason."
)


class EvidenceItem(TypedDict):
    """An observation behind a Memory change: ledger or trace with id, or artifact with ref."""

    kind: memory_changes.EvidenceKind
    id: NotRequired[UUID]
    ref: NotRequired[str]
    observation: str


class AttemptItem(TypedDict):
    attempt: str
    result: NotRequired[str]
    reason: NotRequired[str]
    next: NotRequired[str]


class DecisionItem(TypedDict):
    decision: str
    reason: NotRequired[str]
    supersedes_id: NotRequired[UUID]


class ArtifactItem(TypedDict):
    kind: task_history.ArtifactKind
    locator: str
    label: NotRequired[str]


class _ItemSchema:
    """Show an item type in the tool schema while the argument still takes any items.

    A malformed item then reaches the code's own shape pass and is reported with the
    request's other problems, instead of stopping alone at argument validation.
    """

    def __init__(self, item: type, *, min_items: int = 0):
        self.item = item
        self.min_items = min_items

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> dict[str, Any]:
        from pydantic import TypeAdapter

        schema = {"type": "array", "items": TypeAdapter(self.item).json_schema()}
        if self.min_items:
            schema["minItems"] = self.min_items
        return schema


class _Choice:
    """Show a text argument's values in the tool schema while it still takes any text.

    A value outside them then reaches the code's own shape pass, as an item does through
    `_ItemSchema`.
    """

    def __init__(self, values: tuple[str, ...]):
        self.values = values

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> dict[str, Any]:
        return {"type": "string", "enum": list(self.values)}


EvidenceList = Annotated[list[Any], _ItemSchema(EvidenceItem, min_items=1)]
AttemptList = Annotated[list[Any], _ItemSchema(AttemptItem)]
DecisionList = Annotated[list[Any], _ItemSchema(DecisionItem)]
ArtifactList = Annotated[list[Any], _ItemSchema(ArtifactItem)]


def _state_contract() -> str:
    """The limits every task state write is held to, taken from the code that enforces them."""
    texts = ", ".join(f"{field} {limit}" for field, limit in tasks.TEXT_LIMITS.items())
    return (
        f"Char limits: {texts}; {', '.join(tasks.LIST_FIELDS)} at most "
        f"{tasks.LIST_MAX_ITEMS} items of {tasks.LIST_MAX_CHARS} each. Budgets in estimated "
        f"tokens: card {config.task_card_capacity()}, full state "
        f"{config.task_detail_capacity()}."
    )


def _history_contract() -> str:
    """The limits on checkpoint history, keyed as each item takes them."""
    limits = task_history.HISTORY_LIMITS
    parts = [f"what_changed {limits['what_changed']}"]
    for name, (required, optional) in task_history.ITEM_KEYS.items():
        keys = [key for key in (*required, *optional) if key in limits]
        parts.append(f"{name} {{{', '.join(f'{key} {limits[key]}' for key in keys)}}}")
    return f"History char limits: {'; '.join(parts)}."


def _body_contract() -> str:
    """The ceilings on a new Memory body and its reason, from the code that enforces them."""
    return (
        f"`content` is pushed to every matching session, so it is held to "
        f"{memories.CONTENT_LIMIT} chars and states the rule alone. Put the reason, "
        f"measurements, dates, and history in `why` (at most {memories.WHY_LIMIT} chars): it "
        "is kept on the evidence ledger row that memory_get returns and is never pushed. An "
        "over-limit field is refused with `problems` and `over_limit`."
    )


#: How a refused task write answers.
_REFUSALS = (
    "Every problem found before writing comes back together in `problems`; a field over its "
    "limit is also in `over_limit` with limit and actual. A card or full state over its "
    "budget is refused with `over_by`, the card with a per-component `breakdown`."
)


#: What a task write returns about the scope's card budget instead of a refusal (v3 8).
_CARD_SHARE = (
    "`card_share`, whose `unseated` names the active tasks beside it that now get no card; "
    "propose close only for those whose goal the user has met"
)

#: Names memory_list takes for the layer pushed to every session. Agents guess `global`
#: for it as often as `always`.
ALWAYS_LAYER = ("always", "global")

#: How the tools that execute a user's instruction take its provenance.
_PROVENANCE = (
    "Pass a short exact quote of the user's words as `instruction`, the `conversation_ref` "
    "it came from (a path under the home directory is stored from ~), and a new "
    "`request_id`; a resent request_id returns the first answer. The Agent executes it; "
    "this provenance is not authentication."
)


def actor() -> str:
    """Return the identity configured for this MCP process."""
    return os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


def _instructed(
    instruction: str, conversation_ref: str | None, **acknowledgments: Any
) -> dict[str, Any]:
    """The approval an MCP write carries.

    An agent only ever executes what the user said, so the kind is not the caller's to pass.
    """
    return {
        "kind": "user_instruction",
        "instruction": instruction,
        "conversation_ref": approvals.home_relative(conversation_ref)
        if conversation_ref
        else conversation_ref,
        **acknowledgments,
    }


def _plain(value: Any) -> Any:
    """Turn database values into values accepted by the MCP JSON boundary."""
    if isinstance(value, (UUID, datetime, date)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _task_card_view(row: dict[str, Any]) -> dict[str, Any]:
    """A task row with its state cut to what the bootstrap card carries.

    task_get is the one full-state read, so every other task answer stays card-sized.
    """
    state = row["state"]
    return {
        **{key: value for key, value in row.items() if key != "state"},
        "state": {
            "goal": state["goal"],
            "status_text": state["status_text"],
            "details": {
                "approach": bool(state["approach"]),
                **{field: len(state[field] or []) for field in tasks.LIST_FIELDS},
            },
            "updated_at": state["updated_at"],
            "updated_by": state["updated_by"],
        },
    }


def _failure(
    error: MashuError, *, task_write: str | None = None, task_id: UUID | None = None
) -> dict[str, Any]:
    answer: dict[str, Any] = {"ok": False, "error": str(error)}
    if isinstance(error, DuplicateTaskError):
        answer["candidates"] = [_task_card_view(row) for row in error.candidates]
    elif isinstance(error, StaleStateError):
        answer["current"] = _task_card_view(error.current)
    elif isinstance(error, OverLimitError):
        answer.update(
            field=error.field,
            limit=error.limit,
            actual=error.actual,
            unit=error.unit,
            over_by=error.over_by,
            breakdown=error.breakdown,
        )
        if task_write and error.field == "task bootstrap card":
            answer["refusal_recorded"] = _record_card_refusal(error, task_write, task_id)
    elif isinstance(error, RefusedError) and error.field is not None:
        answer["field"] = error.field
        if error.span is not None:
            answer["span"] = list(error.span)
    elif isinstance(error, MalformedRequestError):
        answer["problems"] = error.problems
        if error.over_limit:
            answer["over_limit"] = error.over_limit
    elif isinstance(error, RetiredConflictError):
        answer["conflicts"] = error.tombstones
    return _plain(answer)


def _record_card_refusal(error: OverLimitError, tool: str, task_id: UUID | None) -> bool:
    """Log a refused card write after its own transaction rolled back (v3 17)."""
    detail = {
        "tool": tool,
        "task_id": str(task_id) if task_id else None,
        "limit": error.limit,
        "actual": error.actual,
        "over_by": error.over_by,
        "unit": error.unit,
        "breakdown": error.breakdown,
    }
    # The refusal is the answer the agent needs; a failed log write must not replace it.
    try:
        with db.transaction() as cur:
            events.record(cur, "task_card_write_refused", actor(), detail=detail)
    except Exception:
        return False
    return True


def _memory_ref(cur: Any, value: UUID | str) -> UUID:
    """A Memory by its full id or by the 8-character prefix the SessionStart hook prints."""
    return references.resolve_id(
        cur, str(value), table="memory", column="memory_id", label="memory"
    )


def _task_ref(cur: Any, value: UUID | str) -> UUID:
    """A task by its full id or by the prefix a card shows."""
    return references.resolve_id(cur, str(value), table="task", column="task_id", label="task")


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
                    "Knowledge is pushed, not searched. `pending` counts candidates in the "
                    "user's review queue (mashu review); leave them to the user. The stored "
                    "quote of a user instruction is provenance rather than authentication. "
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

    @server.tool(
        description=(
            "Record an incident that missing or stale knowledge caused, and show related "
            "evidence.\n\n"
            "An incident is wrong work actually done: a wrong change, a wrong measurement, a "
            "wrong report to the user. A near miss, a slip of care, or the user's displeasure "
            "alone is not one, and a rule written to show regret only adds to the user's "
            "review queue. When the knowledge was there, name where it was and no candidate is "
            "created: `violated_memory_id` for an active Memory delivered to this session, "
            "including a topic rule whose topic was listed but not read (the 8-character id "
            "the hook prints is enough); `ignored_task_id`, with `ignored_decision_id` when one "
            "decision held it, for task state read or not; `ignored_source` for an instruction "
            "file or skill, named as a path from ~ or the repository or as `skill <name>`. "
            "Otherwise use `work` with `task_id` for a one-time fix, such as editing a doc, an "
            "instruction file, or code, and `rule` only for knowledge every future session "
            "must carry; this never admits knowledge directly. A `rule` prevention becomes a "
            f"candidate Memory body, so it is held to {memories.CONTENT_LIMIT} chars and states "
            "the rule alone; put the reason, measurements, dates, and history in `what`."
        )
    )
    def pain_report(
        what: str,
        prevention: str,
        kind: Annotated[str, _Choice(("incident",))] = "incident",
        scope: str | None = None,
        prevention_kind: str = "rule",
        task_id: UUID | None = None,
        violated_memory_id: UUID | str | None = None,
        ignored_task_id: UUID | str | None = None,
        ignored_decision_id: UUID | None = None,
        ignored_source: str | None = None,
    ) -> dict[str, Any]:
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
                    violated_memory_id=(
                        _memory_ref(cur, violated_memory_id) if violated_memory_id else None
                    ),
                    ignored_task_id=_task_ref(cur, ignored_task_id) if ignored_task_id else None,
                    ignored_decision_id=ignored_decision_id,
                    ignored_source=ignored_source,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_list(scope: str | None = None, topic: str | None = None) -> dict[str, Any]:
        """List active knowledge for one named scope, or read one topic's rules.

        Pass exactly one. `scope='always'` (or `global`) lists the Memories pushed to every
        session. With `topic`, read the rules before starting the work its trigger names;
        bootstrap lists topics but never their rules. A topic's `source` names where its
        long form lives, from the vault or a repository root; when it is a vault note,
        `source_path` and `source_exists` say where and whether it is there.
        """
        if (scope is None) == (topic is None):
            return _failure(MashuError("pass exactly one of scope or topic"))
        try:
            with db.transaction() as cur:
                if topic is not None:
                    answer = topics.read_for_session(cur, topic, actor=actor(), session=session)
                    return _plain({"ok": True, **answer})
                row = scopes.get_scope(cur, scope)
                if row is None and scope in ALWAYS_LAYER:
                    found = memories.always_memories(cur)
                    return _plain({"ok": True, "scope": "always", "memories": found})
                row = row or scopes.require_scope(cur, scope, also=ALWAYS_LAYER[:1])
                found = memories.active_memories(cur, scope_id=row["scope_id"])
                return _plain({"ok": True, "scope": row["name"], "memories": found})
        except MashuError as error:
            return _failure(error)

    body_contract = _body_contract()

    @server.tool(
        description=(
            "Create a pending candidate for the successor of your own memory_change_propose "
            "replace, without admitting it.\n\n"
            "It is recorded as your draft, not as a user instruction, and never joins another "
            "candidate, so the proposal fixes this exact body for review. A request from the "
            "user to remember goes to memory_admit with `content`, and a replace the user "
            f"asked for to memory_change_instructed. {body_contract}"
        )
    )
    def memory_nominate(
        content: str, scope: str | None = None, why: str | None = None
    ) -> dict[str, Any]:
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                answer = nominations.nominate_proposed(
                    cur,
                    content=content,
                    actor=actor(),
                    scope_id=scope_id,
                    why=why,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_get(memory_id: UUID | str) -> dict[str, Any]:
        """Read one Memory body, revision, evidence, and its complete retirement record.

        `memory_id` takes the full id or the 8-character prefix the SessionStart hook prints.
        """
        try:
            with db.transaction() as cur:
                row = memories.memory_details(cur, _memory_ref(cur, memory_id))
                if row is None:
                    raise MashuError(f"no memory {memory_id}")
                return _plain({"ok": True, "memory": row})
        except MashuError as error:
            return _failure(error)

    @server.tool(
        description=(
            "Admit a new Memory with explicit instruction provenance.\n\n"
            "Admit only a standing rule the user asked Mashu to keep. A fact about current "
            "work belongs in the task's decisions, and an expiring condition in the user's "
            "Temporary Context (`mashu remember --until`); offer those instead. Choose the "
            "delivery by where forgetting breaks work: `always` anywhere, `scope` in one "
            "place, `topic` in one kind of work. Left out, delivery is the scope routed from "
            "the server's working directory, or always when that directory is unrouted, so "
            "pass it, and tell the user which delivery and scope the answer shows. "
            "Pass `content` (and optional `scope`): the "
            "nomination and admission happen in one call. If it joins an already pending "
            "candidate or repeats invalidated or legacy retired Memory, it stops with "
            "`admitted: false` and returns the nomination to read; then pass that "
            f"`nomination_id` and its `version` as `nomination_version`. {_PROVENANCE} "
            "Invalidated or legacy conflicts need `conflict_ids` and a `conflict_instruction` "
            "in which the user addresses them. "
            "For a rule the user wants read only during one kind of work, pass "
            "`delivery='topic'` and a `topic` name. A topic that does not exist yet is opened "
            "with the rule, never without it, and needs `topic_trigger` (one sentence saying "
            "when to read it); `scope` names where it is listed, or omit it for every session. "
            "`topic_source` optionally names where the new topic's long form lives, as a path "
            "from the vault or repository root with an optional #heading."
            f"\n\n{body_contract} `why` goes with `content`, not with `nomination_id`."
        )
    )
    def memory_admit(
        request_id: UUID,
        instruction: str,
        nomination_id: UUID | None = None,
        nomination_version: int | None = None,
        content: str | None = None,
        conversation_ref: str | None = None,
        conflict_ids: list[UUID] | None = None,
        conflict_instruction: str | None = None,
        delivery: str | None = None,
        scope: str | None = None,
        topic: str | None = None,
        topic_trigger: str | None = None,
        why: str | None = None,
        topic_source: str | None = None,
    ) -> dict[str, Any]:
        if topic_trigger is not None and topic is None:
            return _failure(MashuError("topic_trigger opens the topic named by topic; pass both"))
        if topic_source is not None and topic is None:
            return _failure(MashuError("topic_source belongs to the topic named by topic"))
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
        if why is not None and content is None:
            return _failure(
                MashuError(
                    "why belongs with content; a pending nomination already carries its "
                    "evidence, so admit it by nomination_id without why"
                )
            )
        approval = _instructed(
            instruction,
            conversation_ref,
            conflict_ids=conflict_ids or [],
            conflict_instruction=conflict_instruction,
        )
        try:
            with capacity.doors("mcp"), db.transaction() as cur:
                topic_id, new_topic = None, None
                if topic:
                    listed = scopes.require_scope(cur, scope)["scope_id"] if scope else None
                    topic_id, new_topic = topics.for_filing(
                        cur, topic, topic_trigger, listed, topic_source
                    )
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
                        topic_id=topic_id,
                        new_topic=new_topic,
                        why=why,
                    )
                    return _plain({"ok": answer["admitted"], **answer})
                if new_topic is not None:
                    topic_id = topics.open_instructed(
                        cur, new_topic, approval=approval, actor=actor()
                    )
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
        operation: memory_changes.Operation,
        evidence: EvidenceList,
        change_id: UUID | None = None,
        retirement_kind: memory_changes.RetirementKind | None = None,
        retire_reason: str | None = None,
        successor_nomination_id: UUID | None = None,
        successor_nomination_version: int | None = None,
        successor_settings: dict[str, Any] | None = None,
        relocated_to_kind: str | None = None,
        relocated_to_id: UUID | None = None,
        restore_reason: str | None = None,
    ) -> dict[str, Any]:
        """Create or refresh (with `change_id`) a retire, replace, restore, or redeliver proposal.

        For a change of your own, which stays pending for review. A retire, replace, or
        restore the user instructed takes one memory_change_instructed call instead, and a
        delivery move one memory_redeliver call. Call memory_get first; pass its memory_id,
        current_revision_id, and updated_at as the target fields. Each operation takes only
        its own fields:
        - retire (active Memory): `retire_reason` and `retirement_kind`, one of
          `invalidated` (the content is wrong), `out_of_scope` (the conditions it served
          ended), or `relocated` (moved to Temporary Context; also pass
          `relocated_to_kind='temporary_context'` and `relocated_to_id`).
        - replace (active Memory): `retire_reason`, plus the nomination_id and version of a
          successor from memory_nominate as `successor_nomination_id` and
          `successor_nomination_version` (reread and update if that version changes);
          `retirement_kind` is `superseded` and may be left out.
        - restore (retired Memory): `restore_reason`.
        - redeliver (active Memory): `successor_settings`; the evidence says why.
        Every `evidence` item cites a ledger row or trace by `id`, or an artifact by `ref`,
        and says what it shows in `observation`; an observation alone is refused. Cite the
        user's words in this conversation as an artifact whose `ref` names the conversation,
        quoting them in `observation`. All shape problems come back together in `problems`.

        Replace keeps the old delivery settings unless `successor_settings` gives both
        `delivery` and `scope_id`. For `delivery='topic'` add `topic`: an existing topic
        takes that topic's scope_id or null, and a new topic also needs `topic_trigger`
        (one sentence saying when to read it) with `scope_id` naming where it is listed, or
        null for every session.
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

    @server.tool(
        description=(
            "Apply one exact pending proposal version once the user explicitly asked for it.\n\n"
            "For a proposal already standing; a change the user asks for with no proposal "
            "takes one memory_change_instructed call instead. If the successor changed and no "
            "longer matches that instruction, get a new instruction first. Invalidated or "
            "legacy conflicts need `conflict_ids` and a `conflict_instruction` addressing them, "
            "and restoring a Memory retired as invalidated or legacy needs a "
            f"`reversal_instruction`. {_PROVENANCE}"
        )
    )
    def memory_change_apply(
        change_id: UUID,
        version: int,
        request_id: UUID,
        instruction: str,
        conversation_ref: str | None = None,
        conflict_ids: list[UUID] | None = None,
        conflict_instruction: str | None = None,
        reversal_instruction: str | None = None,
    ) -> dict[str, Any]:
        try:
            with capacity.doors("mcp"), db.transaction() as cur:
                result = memory_changes.apply(
                    cur,
                    change_id,
                    version=version,
                    request_id=request_id,
                    approval=_instructed(
                        instruction,
                        conversation_ref,
                        conflict_ids=conflict_ids or [],
                        conflict_instruction=conflict_instruction,
                        reversal_instruction=reversal_instruction,
                    ),
                    actor=actor(),
                )
                return _plain({"ok": True, **result})
        except MashuError as error:
            return _failure(error)

    @server.tool(
        description=(
            "Move one Memory to the delivery the user instructed, proposed and applied in one "
            "call.\n\n"
            "Use it only for a move the user asked for; your own idea goes to "
            f"memory_change_propose and stays pending. {_PROVENANCE} `delivery` is always, "
            "scope with `scope` (default: the Memory's current scope), or topic with `topic`: an "
            "existing topic keeps its own scope, and a new one also needs `topic_trigger` (one "
            "sentence saying when to read it) with `scope` naming where it is listed, or omit it "
            "for every session. It is recorded as an applied redeliver proposal."
        )
    )
    def memory_redeliver(
        memory_id: UUID | str,
        delivery: str,
        request_id: UUID,
        instruction: str,
        conversation_ref: str,
        scope: str | None = None,
        topic: str | None = None,
        topic_trigger: str | None = None,
    ) -> dict[str, Any]:
        try:
            with capacity.doors("mcp"), db.transaction() as cur:
                result = memory_changes.redeliver_instructed(
                    cur,
                    _memory_ref(cur, memory_id),
                    delivery=delivery,
                    scope_id=scopes.require_scope(cur, scope)["scope_id"] if scope else None,
                    topic=topic,
                    topic_trigger=topic_trigger,
                    request_id=request_id,
                    approval=_instructed(instruction, conversation_ref),
                    actor=actor(),
                )
                return _plain({"ok": True, **result})
        except MashuError as error:
            return _failure(error)

    @server.tool(
        description=(
            "Retire, replace, or restore one Memory as the user instructed, proposed and applied "
            "in one call.\n\n"
            "Use it only for a change the user asked for; your own idea goes to "
            "memory_change_propose and stays pending. The server reads the Memory's current "
            f"revision itself. {_PROVENANCE} Each `operation` takes only its own fields:\n"
            "- retire (active Memory): `retire_reason` and `retirement_kind`, `invalidated` "
            "(the content is wrong) or `out_of_scope` (the conditions it served ended). A move "
            "to Temporary Context is the user's own step (`c` in the Memories TUI); tell them "
            "so.\n"
            "- replace (active Memory): `content`, the successor rule, and optional `why`; the "
            "successor keeps the old delivery, and `retire_reason` defaults to the instruction. "
            f"{body_contract}\n"
            "- restore (retired Memory): `restore_reason`; a Memory retired as invalidated or "
            "legacy also needs `reversal_instruction`, the user's words addressing that "
            "retirement.\n"
            "When the new or restored content repeats Memory retired as invalidated or legacy, "
            "nothing is written and the refusal returns `conflicts`; show them to the user and, "
            "if they address them, resend with their ids as `conflict_ids` and their words as "
            "`conflict_instruction`. All shape problems come back together in `problems`. It is "
            "recorded as an applied proposal with the quote and `conversation_ref` as evidence."
        )
    )
    def memory_change_instructed(
        memory_id: UUID | str,
        operation: Annotated[str, _Choice(memory_changes.INSTRUCTED_OPERATIONS)],
        instruction: str,
        conversation_ref: str,
        request_id: UUID,
        retirement_kind: Annotated[str, _Choice(memory_changes.INSTRUCTED_RETIREMENT_KINDS)]
        | None = None,
        retire_reason: str | None = None,
        content: str | None = None,
        why: str | None = None,
        restore_reason: str | None = None,
        reversal_instruction: str | None = None,
        conflict_ids: list[UUID] | None = None,
        conflict_instruction: str | None = None,
    ) -> dict[str, Any]:
        try:
            with capacity.doors("mcp"), db.transaction() as cur:
                result = memory_changes.change_instructed(
                    cur,
                    _memory_ref(cur, memory_id),
                    operation=operation,
                    request_id=request_id,
                    approval=_instructed(
                        instruction,
                        conversation_ref,
                        conflict_ids=conflict_ids or [],
                        conflict_instruction=conflict_instruction,
                        reversal_instruction=reversal_instruction,
                    ),
                    actor=actor(),
                    retirement_kind=retirement_kind,
                    retire_reason=retire_reason,
                    content=content,
                    why=why,
                    restore_reason=restore_reason,
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

    state_contract = _state_contract()

    @server.tool(
        description=(
            "Create a task; likely duplicates are returned unless `force` is true.\n\n"
            "Open one only for work expected to span sessions, not for a single request or a "
            "step of a larger task, and search with task_search first. Bootstrap delivers a "
            "scope's cards newest activity first up to its card budget and only names the "
            "older ones, so a needless task pushes real ones out of view. Projects are the "
            "user's; when "
            "none fits, ask the user to run `mashu project create`. `goal` names the outcome "
            "and `status_text` says where the task stands; both ride "
            "on every bootstrap card, so details go in approach or next_actions. "
            f"{state_contract} {_REFUSALS} Success and duplicate candidates return the card "
            f"view with the card and detail budgets, and {_CARD_SHARE}; call task_get for "
            "the full state."
        )
    )
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
                answer["candidates"] = [_task_card_view(row) for row in answer["candidates"]]
                return _plain({"ok": True, **_task_card_view(answer)})
        except MashuError as error:
            return _failure(error, task_write="task_create")

    @server.tool()
    def task_get(
        task_id: UUID,
        attempts: bool = False,
        decisions: bool = False,
        artifacts: bool = False,
        checkpoints: bool = False,
    ) -> dict[str, Any]:
        """Get full current state and requested history; call before working on a task.

        The only tool returning full state; it adds card_tokens, card_limit, card_remaining,
        detail_tokens, and detail_limit.
        """
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
        """Search task names and current state, including closed tasks on request.

        Results carry the card view; call task_get for a task's full state.
        """
        try:
            with db.transaction() as cur:
                found = tasks.task_search(
                    cur,
                    query,
                    project=project,
                    include_closed=include_closed,
                    limit=limit,
                )
                return _plain({"ok": True, "tasks": [_task_card_view(row) for row in found]})
        except MashuError as error:
            return _failure(error)

    @server.tool(
        description=(
            "Write a task's state: replace the fields given and append the history they rest "
            "on. The only task state write.\n\n"
            "Hand off to the next session with it: when a try fails, a costly decision is made, "
            "or the approach or next actions change, and before you stop, not after each "
            "commit, since git log carries progress. Compaction comes without warning, so "
            "these points are the ones that survive it. "
            'Omitted fields keep their value; "" or [] clears one. Pass the '
            "read state's `updated_at` as `expect_updated_at`. `name` renames the task unless "
            "it reads like another open one. `goal` names the outcome and `status_text` says "
            "where the task stands, so it changes only when that does; both ride on every "
            "bootstrap card, so details go in approach or next_actions. Optional "
            "`what_changed` is logged with the write and never delivered. Optional history, "
            "all written with the state or not at all: `attempts` for failed tries, "
            "`decisions` for reasons costly to re-derive, and `artifacts` for external "
            "sources. No snapshot is frozen per write; closing the task freezes the final "
            f"state. {state_contract} {_history_contract()} {_REFUSALS} Success returns the "
            f"card view with the card and detail budgets, and {_CARD_SHARE}."
        )
    )
    def task_checkpoint(
        task_id: UUID,
        expect_updated_at: datetime,
        name: str | None = None,
        goal: str | None = None,
        approach: str | None = None,
        status_text: str | None = None,
        open_questions: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
        what_changed: str | None = None,
        attempts: AttemptList | None = None,
        decisions: DecisionList | None = None,
        artifacts: ArtifactList | None = None,
    ) -> dict[str, Any]:
        try:
            with db.transaction() as cur:
                answer = task_history.checkpoint(
                    cur,
                    task_id,
                    actor=actor(),
                    expect_updated_at=expect_updated_at,
                    what_changed=what_changed,
                    name=name,
                    goal=goal,
                    approach=approach,
                    status_text=status_text,
                    open_questions=open_questions,
                    blockers=blockers,
                    next_actions=next_actions,
                    attempts=attempts,
                    decisions=decisions,
                    artifacts=artifacts,
                    session=session,
                )
                return _plain({"ok": True, **_task_card_view(answer)})
        except MashuError as error:
            return _failure(error, task_write="task_checkpoint", task_id=task_id)

    @server.tool(
        description=(
            "Record a proposed outcome and reason without closing the task; the user decides "
            "with `mashu task close`.\n\n`outcome` is "
            + ", ".join(f"`{name}` ({tasks.OUTCOME_MEANINGS[name]})" for name in tasks.OUTCOMES)
            + f". `reason` (required, at most {tasks.PROPOSAL_REASON_MAX} chars) gives the "
            "grounds, such as the merged commit or the task that took over. Propose only when "
            "the goal is met as the user judges it, after their merge or acceptance, not when "
            "your part ends or a branch awaits their check. Checkpoint first and propose last: "
            "a later state write marks the proposal stale and the user must confirm it apart."
        )
    )
    def task_propose_close(
        task_id: UUID,
        outcome: tasks.Outcome,
        reason: str,
    ) -> dict[str, Any]:
        try:
            with db.transaction() as cur:
                answer = tasks.propose_close(
                    cur, task_id, outcome=outcome, reason=reason, actor=actor()
                )
                return _plain({"ok": True, **_task_card_view(answer)})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def task_withdraw_close_proposal(task_id: UUID) -> dict[str, Any]:
        """Remove a close proposal without changing task state."""
        try:
            with db.transaction() as cur:
                answer = tasks.withdraw_proposal(cur, task_id, actor=actor())
                return _plain({"ok": True, **_task_card_view(answer)})
        except MashuError as error:
            return _failure(error)

    return server


def main() -> None:
    """Run the MCP server over stdio."""
    build_server().run()
