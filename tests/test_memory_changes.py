from __future__ import annotations

import asyncio
import re
from pathlib import Path
from uuid import uuid4

import pytest

from conftest import apply_change, propose_change, remember, retire
from mashu import (
    db,
    memories,
    memory_changes,
    nominations,
    scopes,
    server,
    topics,
)
from mashu.errors import MalformedRequestError, MashuError, RefusedError, RetiredConflictError
from mashu.tokens import pushed_cost

RULE = "the storage adapter trusts the signed manifest before selecting a mirror"
UPDATED = "the storage adapter verifies the signed manifest before selecting a mirror"
SUCCESSOR = "validate the export envelope checksum before opening its payload"
INVALIDATED = "old export checksum advice caused rejected payloads"
RESTORE_EVIDENCE = [
    {"kind": "artifact", "ref": "release://adapter-v3", "observation": "the check was removed"}
]
REVERSAL = {"reversal_instruction": "Restore the rule after reversing the prior retirement"}


def _successor(cur, content, kind="user_explicit", **kwargs):
    """A pending candidate standing on an explicit ledger row of its own."""
    cur.execute(
        "INSERT INTO ledger (kind, what, prevention, created_by) "
        "VALUES ('explicit', 'user instruction', %s, 'user') RETURNING ledger_id",
        (content,),
    )
    evidence = [cur.fetchone()["ledger_id"]]
    return nominations.create_nomination(
        cur, content=content, kind=kind, evidence=evidence, actor="agent", **kwargs
    )


def _replace(cur, old, successor, version=None, **kwargs):
    return propose_change(
        cur,
        old,
        "replace",
        ledger_id=successor["evidence"][0],
        retire_reason="the successor rule replaces this one",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=version or successor["version"],
        **kwargs,
    )


def _redeliver(cur, memory, settings, **kwargs):
    return propose_change(cur, memory, "redeliver", successor_settings=settings, **kwargs)


def _topic_settings(name, trigger=None, scope_id=None):
    settings = {"delivery": "topic", "scope_id": scope_id, "topic": name}
    if trigger is not None:
        settings["topic_trigger"] = trigger
    return settings


def test_a_malformed_proposal_reports_every_shape_problem_at_once(cur):
    malformed = {"evidence": [{"observation": "x"}], "restore_reason": "r"}
    with pytest.raises(MalformedRequestError) as refused:
        propose_change(cur, remember(cur, RULE), "replace", **malformed)
    assert server._failure(refused.value)["problems"] == [
        "replace does not take restore_reason",
        "replace needs retire_reason",
        "replace needs successor_nomination_id",
        "replace needs successor_nomination_version, the successor version just read",
        f"evidence[0] needs {memory_changes.EVIDENCE_SHAPE} (missing: kind, id or ref); "
        f"{memory_changes.CONVERSATION_EVIDENCE}",
    ]


def test_agent_proposal_stays_pending_until_explicit_instruction_and_replay_is_idempotent(cur):
    memory = remember(cur, RULE)
    proposal = propose_change(
        cur, memory, "retire", retirement_kind="invalidated", retire_reason="now verified"
    )

    assert proposal["operation"] == "retire"
    assert proposal["target_revision_id"] is not None
    assert proposal["version"] == 1
    assert proposal["target"]["content"] == RULE
    with pytest.raises(MashuError, match="approval kind"):
        memory_changes.apply(
            cur, proposal["change_id"], version=1, request_id=uuid4(), approval={}, actor="agent"
        )
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"
    assert memory_changes.get(cur, proposal["change_id"])["status"] == "pending"

    request_id = uuid4()
    instruction = "Retire this rule because the adapter now checks it"
    with pytest.raises(RefusedError):
        apply_change(cur, proposal, instruction, reversal_instruction="SECRETMARKER9")
    applied = apply_change(cur, proposal, instruction, request_id=request_id)
    replay = apply_change(cur, proposal, instruction, request_id=request_id)

    assert applied == replay
    assert applied["memory"]["status"] == "retired"
    assert applied["memory"]["retirement_kind"] == "invalidated"
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "retired"
    cur.execute(
        "SELECT actor, detail FROM event_log WHERE event_type = 'memory_retired' "
        "AND memory_id = %s",
        (memory["memory_id"],),
    )
    retirement_event = cur.fetchone()
    assert retirement_event["actor"] == "agent"
    assert retirement_event["detail"]["approval_source"]["kind"] == "user_instruction"
    assert retirement_event["detail"]["approval_source"]["instruction"] == instruction
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'memory_change_applied'")
    assert cur.fetchone()["n"] == 1

    with pytest.raises(MashuError, match="different memory change request"):
        apply_change(cur, proposal, "a different instruction", request_id=request_id)


def test_changed_target_revision_requires_a_refreshed_proposal(cur):
    memory = remember(cur, RULE)
    retirement = {"retirement_kind": "invalidated", "retire_reason": "the adapter verifies it"}
    proposal = propose_change(cur, memory, "retire", **retirement)
    memories.revise(cur, memory["memory_id"], content=UPDATED, actor="user")

    with pytest.raises(MashuError, match="target Memory changed"):
        apply_change(cur, proposal)

    refreshed = propose_change(cur, memory, "retire", change_id=proposal["change_id"], **retirement)
    current = memories.memory_details(cur, memory["memory_id"])
    assert refreshed["version"] == 2
    assert refreshed["target_revision_id"] == current["current_revision_id"]
    assert apply_change(cur, refreshed)["memory"]["status"] == "retired"


def test_new_conflict_after_replace_proposal_requires_reread_and_acknowledgment(cur):
    old = remember(cur, RULE)
    successor = _successor(cur, SUCCESSOR)
    proposal = _replace(cur, old, successor)

    invalidated = remember(cur, SUCCESSOR)
    retire(cur, invalidated, INVALIDATED)
    with pytest.raises(MashuError, match="conflicts changed"):
        apply_change(cur, proposal, "Replace the old rule")
    assert memories.get_memory(cur, old["memory_id"])["status"] == "active"
    assert nominations._require_pending(cur, successor["nomination_id"])["status"] == "pending"

    conflict_ids = nominations.current_conflict_ids(cur, SUCCESSOR)
    refreshed_nomination = nominations.refresh_conflicts(
        cur, successor["nomination_id"], conflict_ids, actor="agent"
    )
    refreshed = _replace(
        cur, old, successor, refreshed_nomination["version"], change_id=proposal["change_id"]
    )
    assert refreshed["version"] == 2
    assert refreshed["conflicts"][0]["retirement_kind"] == "invalidated"
    result = apply_change(
        cur,
        refreshed,
        "Replace this rule after considering its invalidation",
        conflict_ids=[invalidated["memory_id"]],
        conflict_instruction="Apply this replacement despite the invalidated rule",
    )
    assert result["operation"] == "replace"
    assert result["retired"]["retirement_kind"] == "superseded"
    assert result["retired"]["superseded_by"] == result["memory"]["memory_id"]
    assert memories.get_memory(cur, old["memory_id"])["status"] == "retired"
    assert refreshed_nomination["conflicts"] == [invalidated["memory_id"]]


def test_memory_get_shows_a_retired_rule_whole_and_restore_keeps_its_history(cur):
    memory = remember(cur, RULE)
    retired = retire(cur, memory, "the adapter was expected to reject unsigned manifests", "legacy")

    detail = memories.memory_details(cur, memory["memory_id"])
    assert detail["content"] == RULE
    assert detail["current_revision_id"] is not None
    assert detail["basis"][0]["ledger_id"] == memory["evidence"][0]
    assert detail["retirement"]["kind"] == "legacy"
    assert detail["retirement"]["reason"] == retired["retire_reason"]
    assert detail["retirement_history"][-1]["event_type"] == "memory_retired"

    proposal = propose_change(
        cur, memory, "restore", restore_reason="the check was removed", evidence=RESTORE_EVIDENCE
    )
    result = apply_change(cur, proposal, "Restore this rule", **REVERSAL)
    assert result["memory"]["status"] == "active"
    assert result["memory"]["retirement_kind"] is None
    history = memories.memory_details(cur, memory["memory_id"])["retirement_history"]
    assert [row["event_type"] for row in history] == ["memory_retired", "memory_restored"]
    assert history[0]["detail"]["reason"] == retired["retire_reason"]


def test_restore_refuses_a_duplicate_active_memory(cur):
    memory = remember(cur, RULE)
    retire(cur, memory, "this rule is no longer needed", "legacy")
    remember(cur, RULE, acknowledged_conflicts=[memory["memory_id"]])
    proposal = propose_change(
        cur, memory, "restore", restore_reason="the path returned", evidence=RESTORE_EVIDENCE
    )

    with pytest.raises(MashuError, match="similar active Memory already exists"):
        apply_change(cur, proposal, "Restore the returned rule", **REVERSAL)
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "retired"


def test_replace_seats_against_the_final_active_set_and_refusal_rolls_back(cur, monkeypatch):
    old_content = "oldrule " + "amber brass cedar cobalt copper " * 3
    new_content = "newrule apricot birch"
    old_cost = pushed_cost([old_content])
    assert pushed_cost([new_content]) < old_cost
    monkeypatch.setenv("MASHU_CAPACITY", str(old_cost))
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(old_cost))
    old = remember(cur, old_content)
    proposal = _replace(cur, old, _successor(cur, new_content, "incident"))
    result = apply_change(cur, proposal, "Replace the larger rule with the shorter one")
    assert result["memory"]["status"] == "active"
    assert result["retired"]["status"] == "retired"

    second_old = remember(cur, "older rule for a separate export format")
    too_large = "too_large " + "violet walnut xylophone zephyr quartz " * 2
    second_successor = _successor(cur, too_large, "incident")
    second_proposal = _replace(cur, second_old, second_successor)
    monkeypatch.setenv("MASHU_CAPACITY", str(pushed_cost([too_large]) - 1))
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(pushed_cost([too_large]) - 1))
    with pytest.raises(RefusedError):
        apply_change(cur, second_proposal, "Replace the old rule")
    assert memories.get_memory(cur, second_old["memory_id"])["status"] == "active"
    pending = nominations._require_pending(cur, second_successor["nomination_id"])
    assert pending["status"] == "pending"
    assert memory_changes.get(cur, second_proposal["change_id"])["status"] == "pending"


def test_replace_proposal_version_follows_its_successor_and_its_delivery(cur):
    old_scope = scopes.create_scope(cur, name=f"old delivery scope {uuid4()}", actor="user")
    new_scope = scopes.create_scope(cur, name=f"new delivery scope {uuid4()}", actor="user")
    old = remember(
        cur, f"old adapter rule {uuid4()}", delivery="scope", scope_id=old_scope["scope_id"]
    )
    revised_content = f"verify the transport envelope before sending {uuid4()}"
    successor = _successor(cur, f"verify the export digest before sending {uuid4()}")
    proposal = _replace(cur, old, successor)
    revised = nominations.revise(
        cur, successor["nomination_id"], content=revised_content, actor="user"
    )
    with pytest.raises(MashuError, match="successor nomination version changed"):
        _replace(cur, old, successor)

    with pytest.raises(MashuError, match="successor nomination changed"):
        apply_change(cur, proposal, "Replace the adapter rule")
    assert memories.get_memory(cur, old["memory_id"])["status"] == "active"
    pending = nominations._require_pending(cur, successor["nomination_id"])
    assert pending["content"] == revised_content

    refreshed = _replace(cur, old, successor, revised["version"], change_id=proposal["change_id"])
    assert refreshed["version"] == proposal["version"] + 1
    assert refreshed["successor_snapshot"]["content"] == revised_content

    moved = {"delivery": "scope", "scope_id": new_scope["scope_id"]}
    changed = _replace(
        cur,
        old,
        successor,
        revised["version"],
        successor_settings=moved,
        change_id=proposal["change_id"],
    )
    assert changed["version"] == refreshed["version"] + 1
    with pytest.raises(MashuError, match="version changed"):
        apply_change(cur, refreshed, "Replace it and move delivery to the new scope")
    applied = apply_change(cur, changed, "Replace it and move delivery to the new scope")
    assert applied["memory"]["content"] == revised_content
    assert applied["memory"]["delivery"] == "scope"
    assert applied["memory"]["scope_id"] == str(new_scope["scope_id"])


def test_replace_inherits_topic_settings_clears_nomination_scope_and_refuses_guard(cur):
    scope = scopes.create_scope(cur, name=f"candidate-only scope {uuid4()}", actor="user")
    home = topics.create_topic(cur, name="deploy", trigger="Before deploying", actor="user")
    old = remember(
        cur, f"run the deployment checksum {uuid4()}", delivery="topic", topic_id=home["topic_id"]
    )
    successor = _successor(
        cur, f"verify the release signature {uuid4()}", "incident", scope_id=scope["scope_id"]
    )
    with pytest.raises(MashuError, match="must be one of always, scope, topic"):
        _replace(cur, old, successor, successor_settings={"delivery": "guard", "scope_id": None})
    proposal = _replace(cur, old, successor)
    assert proposal["successor_delivery"] == "topic"
    assert proposal["successor_scope_id"] is None

    applied = apply_change(cur, proposal, "Replace it with the release signature check")
    assert applied["memory"]["delivery"] == "topic"
    assert applied["memory"]["scope_id"] is None
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'memory_change_applied' "
        "AND memory_id = %s ORDER BY event_id DESC LIMIT 1",
        (old["memory_id"],),
    )
    assert cur.fetchone()["detail"]["successor_settings"] == {
        "delivery": "topic",
        "scope_id": None,
        "topic_id": str(home["topic_id"]),
        "topic_name": "deploy",
    }


def test_redeliver_moves_a_rule_into_an_existing_topic_without_touching_its_body(cur):
    memory = remember(cur, RULE)
    with pytest.raises(MashuError, match="as they are"):
        _redeliver(cur, memory, {"delivery": "always", "scope_id": None})
    topics.create_topic(cur, name="calibration", trigger="Before calibrating", actor="user")
    with pytest.raises(MashuError, match="already exists"):
        _redeliver(cur, memory, _topic_settings("calibration", "Before calibrating"))
    with pytest.raises(MashuError, match="only changes delivery settings"):
        _redeliver(cur, memory, _topic_settings("calibration"), retire_reason="no")

    proposal = _redeliver(cur, memory, _topic_settings("calibration"))
    assert proposal["delivery_move"] == ["delivery: always -> topic:calibration"]
    assert memories.get_memory(cur, memory["memory_id"])["delivery"] == "always"

    request_id = uuid4()
    result = apply_change(cur, proposal, "Move it into the topic", request_id=request_id)
    assert apply_change(cur, proposal, "Move it into the topic", request_id=request_id) == result

    moved = memories.get_memory(cur, memory["memory_id"])
    assert result["operation"] == "redeliver"
    assert moved["delivery"] == "topic"
    assert moved["content"] == RULE
    assert moved["memory_id"] == memory["memory_id"]
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'delivery_changed' AND memory_id = %s",
        (memory["memory_id"],),
    )
    assert cur.fetchone()["detail"]["approval_source"]["kind"] == "user_instruction"


def test_redeliver_opens_a_described_topic_only_when_it_fits(cur, scope_id, monkeypatch):
    memory = remember(cur, RULE)
    proposal = _redeliver(
        cur, memory, _topic_settings("calibration", "Before calibrating", scope_id)
    )
    assert proposal["delivery_move"][1] == (
        "opens topic calibration for test scope: Before calibrating"
    )

    monkeypatch.setenv("MASHU_TOPIC_CAPACITY", "1")
    with pytest.raises(RefusedError), cur.connection.transaction():
        apply_change(cur, proposal, "Put it in a new calibration topic")
    assert topics.get_topic(cur, "calibration") is None
    assert memories.get_memory(cur, memory["memory_id"])["delivery"] == "always"
    assert memory_changes.get(cur, proposal["change_id"])["status"] == "pending"

    monkeypatch.delenv("MASHU_TOPIC_CAPACITY")
    apply_change(cur, proposal, "Put it in a new calibration topic")

    topic = topics.get_topic(cur, "calibration")
    moved = memories.get_memory(cur, memory["memory_id"])
    assert topic["scope_id"] == scope_id
    assert topic["created_by"] == "agent"
    assert moved["topic_id"] == topic["topic_id"]
    assert moved["scope_id"] == scope_id


def test_redeliver_refuses_a_topic_opened_differently_or_a_target_changed_after_it(cur, scope_id):
    memory = remember(cur, RULE)
    proposal = _redeliver(cur, memory, _topic_settings("calibration", "Before calibrating"))
    topics.create_topic(cur, name="calibration", trigger="Before tuning", actor="user")
    with pytest.raises(MashuError, match="opened differently"):
        apply_change(cur, proposal, "Put it in a new calibration topic")
    assert memories.get_memory(cur, memory["memory_id"])["delivery"] == "always"

    scoped = {"delivery": "scope", "scope_id": scope_id}
    proposal = _redeliver(cur, memory, scoped)
    memories.revise(cur, memory["memory_id"], content=UPDATED, actor="user")
    with pytest.raises(MashuError, match="changed after this proposal was read"):
        apply_change(cur, proposal, "Scope it")


def test_mcp_records_a_user_instruction_without_being_told_its_kind(mcp, dsn):
    tools = {tool.name: tool for tool in asyncio.run(server.build_server().list_tools())}
    for name in ("memory_admit", "memory_change_apply", "memory_redeliver"):
        assert "approval_kind" not in tools[name].input_schema["properties"], name
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
        proposal = propose_change(
            cur, memory, "retire", retirement_kind="invalidated", retire_reason="now verified"
        )

    admitted = mcp("memory_admit", request_id=str(uuid4()), instruction="do it", content=SUCCESSOR)
    applied = mcp(
        "memory_change_apply",
        change_id=str(proposal["change_id"]),
        version=proposal["version"],
        request_id=str(uuid4()),
        instruction="do it",
    )

    assert admitted["ok"] is True and applied["ok"] is True
    with db.transaction(dsn) as cur:
        change = memory_changes.get(cur, proposal["change_id"])
        cur.execute("SELECT approval_source FROM nomination")
        nominated = cur.fetchone()["approval_source"]
    assert change["approval_source"]["kind"] == nominated["kind"] == "user_instruction"


def test_memory_redeliver_proposes_and_applies_an_instructed_move_in_one_call(
    mcp, dsn, monkeypatch
):
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
    call = {
        "memory_id": str(memory["memory_id"]),
        "request_id": str(uuid4()),
        "instruction": "Move it into a calibration topic",
        "conversation_ref": "conversation:turn-7",
        "delivery": "topic",
        "topic": "calibration",
    }
    assert "topic_trigger" in mcp("memory_redeliver", **call)["error"]
    monkeypatch.setenv("MASHU_TOPIC_CAPACITY", "1")
    assert mcp("memory_redeliver", **call, topic_trigger="Before calibrating")["ok"] is False
    monkeypatch.delenv("MASHU_TOPIC_CAPACITY")
    with db.transaction(dsn) as cur:
        assert topics.get_topic(cur, "calibration") is None
        assert cur.execute("SELECT count(*) AS n FROM memory_change").fetchone()["n"] == 0

    moved = mcp("memory_redeliver", **call, topic_trigger="Before calibrating")
    assert moved["ok"] is True
    assert mcp("memory_redeliver", **call, topic_trigger="Before calibrating") == moved
    elsewhere = {**{k: v for k, v in call.items() if k != "topic"}, "delivery": "always"}
    assert "different memory change request" in mcp("memory_redeliver", **elsewhere)["error"]

    with db.transaction(dsn) as cur:
        change = memory_changes.get(cur, moved["change_id"])
        cur.execute("SELECT event_type FROM event_log WHERE memory_id = %s", (memory["memory_id"],))
        logged = {row["event_type"] for row in cur.fetchall()}
        topic = topics.get_topic(cur, "calibration")
    assert change["status"] == "applied" and change["operation"] == "redeliver"
    assert change["evidence"][0]["ref"] == "conversation:turn-7"
    assert "Move it into a calibration topic" in change["evidence"][0]["observation"]
    assert change["approval_source"]["conversation_ref"] == "conversation:turn-7"
    assert {"memory_change_proposed", "delivery_changed", "memory_change_applied"} <= logged
    assert change["target"]["topic_id"] == topic["topic_id"]


#: What both ways of making a change leave differently: their own ids and times, and the
#: one-call request stored for its replay.
_PER_CALL = {
    "change_id",
    "target_memory_id",
    "target_revision_id",
    "target_updated_at",
    "proposed_at",
    "decided_at",
    "apply_request_id",
    "apply_request",
    "applied_result",
    "successor_nomination_id",
    "successor_snapshot",
    "superseded_by",
}
REF = "conversation:turn-9"


def _approval(instruction, **acknowledgments):
    return {
        "kind": "user_instruction",
        "instruction": instruction,
        "conversation_ref": REF,
        "conflict_ids": [],
        "conflict_instruction": None,
        "reversal_instruction": None,
        **acknowledgments,
    }


def _instructed(cur, memory, operation, approval, request_id=None, **fields):
    return memory_changes.change_instructed(
        cur,
        memory["memory_id"],
        operation=operation,
        request_id=request_id or uuid4(),
        approval=approval,
        actor="agent",
        **fields,
    )


@pytest.mark.parametrize("operation", ["retire", "replace", "restore"])
def test_an_instructed_change_leaves_what_proposing_and_applying_it_leaves(cur, operation):
    instruction = f"Please {operation} that rule"
    approval = _approval(instruction)
    evidence = [
        {
            "kind": "artifact",
            "ref": REF,
            "observation": f"the user instructed this {operation}: {instruction}",
        }
    ]
    targets = (RULE, "run the schema migration dry run before every deploy")
    successors = (SUCCESSOR, "pin the compiler version in every nightly build image")
    rows, logs = [], []
    for one_call, target, successor in zip((False, True), targets, successors, strict=True):
        memory = remember(cur, target)
        if operation == "restore":
            retire(cur, memory, "the adapter no longer exists", "out_of_scope")
        fields = {
            "retire": {"retirement_kind": "invalidated", "retire_reason": "it is wrong"},
            "replace": {"content": successor, "retire_reason": "the user rewrote it"},
            "restore": {"restore_reason": "the adapter is back"},
        }[operation]
        cur.execute("SELECT coalesce(max(event_id), 0) AS n FROM event_log")
        before = cur.fetchone()["n"]
        if one_call:
            change_id = _instructed(cur, memory, operation, approval, **fields)["change_id"]
        else:
            if operation == "replace":
                nominated = nominations.nominate_user_explicit(
                    cur, content=fields.pop("content"), actor="agent"
                )["nomination"]
                fields.update(
                    successor_nomination_id=nominated["nomination_id"],
                    successor_nomination_version=nominated["version"],
                )
            change = propose_change(cur, memory, operation, evidence=evidence, **fields)
            memory_changes.apply(
                cur,
                change["change_id"],
                version=change["version"],
                request_id=uuid4(),
                approval=approval,
                actor="agent",
            )
            change_id = change["change_id"]
        cur.execute("SELECT * FROM memory_change WHERE change_id = %s", (change_id,))
        rows.append({k: v for k, v in cur.fetchone().items() if k not in _PER_CALL})
        cur.execute(
            "SELECT event_type, actor, detail FROM event_log WHERE event_id > %s ORDER BY event_id",
            (before,),
        )
        logs.append([(e["event_type"], e["actor"], sorted(e["detail"] or {})) for e in cur])

    assert rows[0] == rows[1]
    assert rows[1]["status"] == "applied" and rows[1]["evidence"] == evidence
    assert logs[0] == logs[1]


def test_an_instructed_replace_stops_on_an_unaddressed_conflict_and_answers_a_resend_alike(cur):
    old = remember(cur, RULE)
    invalidated = remember(cur, SUCCESSOR)
    retire(cur, invalidated, INVALIDATED)
    approval = _approval("Replace it with the checksum rule")

    with pytest.raises(RetiredConflictError) as stopped, cur.connection.transaction():
        _instructed(cur, old, "replace", approval, content=SUCCESSOR)
    assert server._failure(stopped.value)["conflicts"] == [
        {
            "memory_id": str(invalidated["memory_id"]),
            "retirement_kind": "invalidated",
            "retire_reason": INVALIDATED,
            "superseded_by": None,
            "relocated_to_kind": None,
            "relocated_to_id": None,
        }
    ]
    cur.execute("SELECT count(*) AS n FROM nomination")
    assert cur.fetchone()["n"] == 0

    addressed = {
        **approval,
        "conflict_ids": [invalidated["memory_id"]],
        "conflict_instruction": "Use it even though the old checksum advice was invalidated",
    }
    request_id = uuid4()
    first = _instructed(cur, old, "replace", addressed, request_id, content=SUCCESSOR)
    assert _instructed(cur, old, "replace", addressed, request_id, content=SUCCESSOR) == first
    assert first["retired"]["superseded_by"] == first["memory"]["memory_id"]
    assert first["retired"]["retire_reason"] == (
        "replaced on the user's instruction: Replace it with the checksum rule"
    )
    with pytest.raises(MashuError, match="different memory change request"):
        _instructed(cur, old, "replace", addressed, request_id, content=UPDATED)


def test_an_instructed_restore_needs_the_reversal_and_conflicts_its_retirement_calls_for(cur):
    memory = remember(cur, RULE)
    retire(cur, memory, "the adapter was expected to reject unsigned manifests")
    similar = remember(cur, UPDATED, acknowledged_conflicts=[memory["memory_id"]])
    retire(cur, similar, INVALIDATED)
    approval = _approval("Bring the manifest rule back")
    restore = {"restore_reason": "unsigned manifests are accepted again"}

    with pytest.raises(RetiredConflictError) as stopped, cur.connection.transaction():
        _instructed(cur, memory, "restore", approval, **restore)
    assert [row["memory_id"] for row in stopped.value.tombstones] == [str(similar["memory_id"])]
    addressed = {
        **approval,
        "conflict_ids": [similar["memory_id"]],
        "conflict_instruction": "Restore it even though the revised rule was withdrawn",
    }
    with pytest.raises(MashuError, match="reversal_instruction"), cur.connection.transaction():
        _instructed(cur, memory, "restore", addressed, **restore)

    reversed_ = {**addressed, **REVERSAL}
    restored = _instructed(cur, memory, "restore", reversed_, **restore)
    assert restored["memory"]["status"] == "active"
    assert (
        memory_changes.get(cur, restored["change_id"])["approval_source"]["reversal_instruction"]
        == REVERSAL["reversal_instruction"]
    )


def test_memory_change_instructed_takes_the_users_words_in_one_call(mcp, dsn, monkeypatch):
    monkeypatch.setenv("HOME", "/Users/SECRETMARKER1")
    transcript = "/Users/SECRETMARKER1/.claude/projects/-Users-SECRETMARKER1-adapter/a.jsonl"
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
    call = {
        "memory_id": str(memory["memory_id"]),
        "operation": "retire",
        "instruction": "Retire the manifest rule, it is wrong",
        "conversation_ref": transcript,
        "request_id": str(uuid4()),
    }

    malformed = mcp("memory_change_instructed", **call, retirement_kind="relocated", content="x")
    assert malformed["problems"] == [
        "retire does not take content",
        "retire needs retire_reason",
        "a relocated retirement names its Temporary Context, so propose it with "
        "memory_change_propose",
    ]
    retirement = {"retirement_kind": "invalidated", "retire_reason": "the adapter verifies it"}
    banned = {**call, "conversation_ref": "conversation:SECRETMARKER2"}
    assert mcp("memory_change_instructed", **banned, **retirement)["field"] == "conversation_ref"

    retired = mcp("memory_change_instructed", **call, **retirement)
    assert retired["ok"] is True and retired["memory"]["status"] == "retired"
    assert mcp("memory_change_instructed", **call, **retirement) == retired
    with db.transaction(dsn) as cur:
        change = memory_changes.get(cur, retired["change_id"])
        cur.execute("SELECT count(*) AS n FROM memory_change")
        assert cur.fetchone()["n"] == 1
    cited = "~/.claude/projects/~-adapter/a.jsonl"
    assert change["evidence"][0]["ref"] == change["approval_source"]["conversation_ref"] == cited


def test_an_evidence_reference_keeps_the_home_directory_from_tilde(cur, monkeypatch, tmp_path):
    home = str(Path.home())
    (tmp_path / "patterns").write_text(re.escape(home) + "\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "patterns"))
    evidence = [{"kind": "artifact", "ref": f"{home}/notes/a.md", "observation": "it moved"}]

    change = propose_change(
        cur,
        remember(cur, RULE),
        "retire",
        evidence=evidence,
        retirement_kind="out_of_scope",
        retire_reason="the conditions it served ended",
    )

    assert change["evidence"][0]["ref"] == "~/notes/a.md"
