from __future__ import annotations

from uuid import uuid4

import pytest

from mashu import bootstrap, match, memories, memory_changes, nominations, scopes
from mashu.errors import MashuError, RefusedError
from mashu.tokens import pushed_cost

RULE = "the storage adapter trusts the signed manifest before selecting a mirror"
UPDATED = "the storage adapter verifies the signed manifest before selecting a mirror"
SUCCESSOR = "validate the export envelope checksum before opening its payload"
INVALIDATED = "old export checksum advice caused rejected payloads"


def _memory(cur, content: str = RULE):
    return memories.remember(cur, content=content, actor="user")


def _evidence(cur, content: str = "observed the storage adapter outcome"):
    cur.execute(
        """
        INSERT INTO ledger (kind, what, prevention, created_by)
        VALUES ('explicit', 'user instruction', %s, 'user')
        RETURNING ledger_id
        """,
        (content,),
    )
    return cur.fetchone()["ledger_id"]


def _propose_retire(cur, memory, *, kind="invalidated", reason="the adapter now verifies it"):
    detail = memories.memory_details(cur, memory["memory_id"])
    return memory_changes.propose(
        cur,
        target_memory_id=memory["memory_id"],
        target_revision_id=detail["current_revision_id"],
        target_updated_at=detail["updated_at"],
        operation="retire",
        retirement_kind=kind,
        retire_reason=reason,
        evidence=[
            {
                "kind": "ledger",
                "id": str(memory["evidence"][0]),
                "observation": "the adapter now verifies the rule before loading",
            }
        ],
        actor="agent",
    )


def _approval(instruction: str = "Retire this rule because the adapter now checks it"):
    return {
        "kind": "user_instruction",
        "instruction": instruction,
        "conversation_ref": "conversation:turn-42",
    }


def test_memory_get_returns_full_retired_memory_without_putting_body_in_matches(cur):
    memory = _memory(cur)
    retired = memories.retire(
        cur,
        memory["memory_id"],
        reason="the storage adapter enforces this itself",
        retirement_kind="invalidated",
        actor="user",
    )

    detail = memories.memory_details(cur, memory["memory_id"])
    assert detail["content"] == RULE
    assert detail["current_revision_id"] is not None
    assert detail["basis"][0]["ledger_id"] == memory["evidence"][0]
    assert detail["retirement"]["kind"] == "invalidated"
    assert detail["retirement"]["reason"] == retired["retire_reason"]
    assert detail["retirement_history"][-1]["event_type"] == "memory_retired"
    assert "content" not in match.similar_tombstones(cur, RULE)[0]
    assert RULE not in str(bootstrap.session_bootstrap(cur, actor="agent"))


def test_agent_proposal_stays_pending_until_explicit_instruction_and_replay_is_idempotent(cur):
    memory = _memory(cur)
    proposal = _propose_retire(cur, memory)

    assert proposal["operation"] == "retire"
    assert proposal["target_revision_id"] is not None
    assert proposal["version"] == 1
    assert proposal["target"]["content"] == RULE
    with pytest.raises(MashuError, match="approval kind"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=1,
            request_id=uuid4(),
            approval={},
            actor="agent",
        )
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"
    assert memory_changes.get(cur, proposal["change_id"])["status"] == "pending"

    request_id = uuid4()
    approval = _approval()
    applied = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=1,
        request_id=request_id,
        approval=approval,
        actor="agent",
    )
    replay = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=1,
        request_id=request_id,
        approval=approval,
        actor="agent",
    )

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
    assert retirement_event["detail"]["approval_source"]["instruction"] == approval["instruction"]
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'memory_change_applied'")
    assert cur.fetchone()["n"] == 1

    with pytest.raises(MashuError, match="different memory change request"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=1,
            request_id=request_id,
            approval=_approval("a different instruction"),
            actor="agent",
        )


def test_changed_target_revision_requires_a_refreshed_proposal(cur):
    memory = _memory(cur)
    proposal = _propose_retire(cur, memory)
    memories.revise(cur, memory["memory_id"], content=UPDATED, actor="user")

    with pytest.raises(MashuError, match="target Memory changed"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=1,
            request_id=uuid4(),
            approval=_approval(),
            actor="agent",
        )

    current = memories.memory_details(cur, memory["memory_id"])
    refreshed = memory_changes.propose(
        cur,
        target_memory_id=memory["memory_id"],
        target_revision_id=current["current_revision_id"],
        target_updated_at=current["updated_at"],
        operation="retire",
        retirement_kind="invalidated",
        retire_reason="the adapter now verifies the updated rule",
        evidence=[
            {
                "kind": "ledger",
                "id": str(memory["evidence"][0]),
                "observation": "the adapter now verifies the updated rule",
            }
        ],
        actor="agent",
        change_id=proposal["change_id"],
    )
    assert refreshed["version"] == 2
    assert refreshed["target_revision_id"] == current["current_revision_id"]
    result = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=2,
        request_id=uuid4(),
        approval=_approval("Retire the updated rule because the adapter now verifies it"),
        actor="agent",
    )
    assert result["memory"]["status"] == "retired"


def test_new_conflict_after_replace_proposal_requires_reread_and_acknowledgment(cur):
    old = _memory(cur)
    ledger_id = _evidence(cur, SUCCESSOR)
    successor = nominations.create_nomination(
        cur,
        content=SUCCESSOR,
        kind="user_explicit",
        evidence=[ledger_id],
        actor="agent",
    )
    target = memories.memory_details(cur, old["memory_id"])
    proposal = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=target["current_revision_id"],
        target_updated_at=target["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the new export envelope rule replaces this one",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
        evidence=[
            {"kind": "ledger", "id": str(ledger_id), "observation": "new incident needs this rule"}
        ],
        actor="agent",
    )

    invalidated = _memory(cur, SUCCESSOR)
    memories.retire(
        cur,
        invalidated["memory_id"],
        reason=INVALIDATED,
        retirement_kind="invalidated",
        actor="user",
    )
    with pytest.raises(MashuError, match="conflicts changed"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=1,
            request_id=uuid4(),
            approval=_approval("Replace the old rule"),
            actor="agent",
        )
    assert memories.get_memory(cur, old["memory_id"])["status"] == "active"
    assert nominations._require_pending(cur, successor["nomination_id"])["status"] == "pending"

    conflict_ids = nominations.current_conflict_ids(cur, SUCCESSOR)
    refreshed_nomination = nominations.refresh_conflicts(
        cur, successor["nomination_id"], conflict_ids, actor="agent"
    )
    current = memories.memory_details(cur, old["memory_id"])
    refreshed = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=current["current_revision_id"],
        target_updated_at=current["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the new export envelope rule replaces this one",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=refreshed_nomination["version"],
        evidence=[
            {"kind": "ledger", "id": str(ledger_id), "observation": "new incident needs this rule"}
        ],
        actor="agent",
        change_id=proposal["change_id"],
    )
    assert refreshed["version"] == 2
    assert refreshed["conflicts"][0]["retirement_kind"] == "invalidated"
    result = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=2,
        request_id=uuid4(),
        approval={
            **_approval("Replace this rule after considering its invalidation"),
            "conflict_ids": [invalidated["memory_id"]],
            "conflict_instruction": "Apply this replacement despite the invalidated rule",
        },
        actor="agent",
    )
    assert result["operation"] == "replace"
    assert result["retired"]["retirement_kind"] == "superseded"
    assert result["retired"]["superseded_by"] == result["memory"]["memory_id"]
    assert memories.get_memory(cur, old["memory_id"])["status"] == "retired"
    assert refreshed_nomination["conflicts"] == [invalidated["memory_id"]]


def test_restore_rechecks_capacity_duplicates_and_keeps_retirement_history(cur):
    memory = _memory(cur)
    retired = memories.retire(
        cur,
        memory["memory_id"],
        reason="the adapter was expected to reject unsigned manifests",
        retirement_kind="legacy",
        actor="user",
        _legacy_compat=True,
    )
    detail = memories.memory_details(cur, memory["memory_id"])
    proposal = memory_changes.propose(
        cur,
        target_memory_id=memory["memory_id"],
        target_revision_id=detail["current_revision_id"],
        target_updated_at=detail["updated_at"],
        operation="restore",
        restore_reason="the upstream signature check was removed",
        evidence=[
            {
                "kind": "artifact",
                "ref": "release://adapter-v3",
                "observation": "the upstream release removed the replacement check",
            }
        ],
        actor="agent",
    )

    result = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=1,
        request_id=uuid4(),
        approval={
            **_approval("Restore this rule because the upstream check was removed"),
            "reversal_instruction": "Restore the rule after reversing the prior retirement",
        },
        actor="agent",
    )
    assert result["memory"]["status"] == "active"
    assert result["memory"]["retirement_kind"] is None
    history = memories.memory_details(cur, memory["memory_id"])["retirement_history"]
    assert [row["event_type"] for row in history] == ["memory_retired", "memory_restored"]
    assert history[0]["detail"]["reason"] == retired["retire_reason"]


def test_restore_refuses_a_duplicate_active_memory(cur):
    memory = _memory(cur)
    memories.retire(
        cur,
        memory["memory_id"],
        reason="this rule is no longer needed",
        retirement_kind="legacy",
        actor="user",
        _legacy_compat=True,
    )
    memories.remember(
        cur,
        content=RULE,
        actor="user",
        acknowledged_conflicts=[memory["memory_id"]],
    )
    detail = memories.memory_details(cur, memory["memory_id"])
    proposal = memory_changes.propose(
        cur,
        target_memory_id=memory["memory_id"],
        target_revision_id=detail["current_revision_id"],
        target_updated_at=detail["updated_at"],
        operation="restore",
        restore_reason="the old deployment path returned",
        evidence=[
            {
                "kind": "artifact",
                "ref": "release://adapter-v4",
                "observation": "the older deployment path has returned",
            }
        ],
        actor="agent",
    )

    with pytest.raises(MashuError, match="similar active Memory already exists"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=1,
            request_id=uuid4(),
            approval={
                **_approval("Restore the returned rule"),
                "reversal_instruction": "Reverse the legacy retirement after reviewing its reason",
            },
            actor="agent",
        )
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "retired"


def test_replace_seats_against_the_final_active_set_and_refusal_rolls_back(cur, monkeypatch):
    old_content = "oldrule " + "amber brass cedar cobalt copper " * 18
    new_content = "newrule " + "apricot birch daisy emerald fig " * 9
    old_cost = pushed_cost([old_content])
    new_cost = pushed_cost([new_content])
    assert new_cost < old_cost
    monkeypatch.setenv("MASHU_CAPACITY", str(old_cost))
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(old_cost))
    old = memories.remember(cur, content=old_content, actor="user")
    evidence = _evidence(cur, new_content)
    successor = nominations.create_nomination(
        cur,
        content=new_content,
        kind="incident",
        evidence=[evidence],
        actor="agent",
    )
    detail = memories.memory_details(cur, old["memory_id"])
    proposal = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=detail["current_revision_id"],
        target_updated_at=detail["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the shorter checksum rule replaces the larger rule",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
        evidence=[
            {
                "kind": "ledger",
                "id": str(evidence),
                "observation": "the shorter signature rule covers the new issue",
            }
        ],
        actor="agent",
    )
    result = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=1,
        request_id=uuid4(),
        approval=_approval("Replace the larger rule with the shorter one"),
        actor="agent",
    )
    assert result["memory"]["status"] == "active"
    assert result["retired"]["status"] == "retired"

    second_old = _memory(cur, "older rule for a separate export format")
    too_large = "too_large " + "violet walnut xylophone zephyr quartz " * 18
    second_evidence = _evidence(cur, too_large)
    second_successor = nominations.create_nomination(
        cur,
        content=too_large,
        kind="incident",
        evidence=[second_evidence],
        actor="agent",
    )
    second_detail = memories.memory_details(cur, second_old["memory_id"])
    second_proposal = memory_changes.propose(
        cur,
        target_memory_id=second_old["memory_id"],
        target_revision_id=second_detail["current_revision_id"],
        target_updated_at=second_detail["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the oversized replacement should fail without changing either row",
        successor_nomination_id=second_successor["nomination_id"],
        successor_nomination_version=second_successor["version"],
        evidence=[
            {
                "kind": "ledger",
                "id": str(second_evidence),
                "observation": "the proposed rule exceeds the remaining seat",
            }
        ],
        actor="agent",
    )
    monkeypatch.setenv("MASHU_CAPACITY", str(pushed_cost([too_large]) - 1))
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(pushed_cost([too_large]) - 1))
    with pytest.raises(RefusedError):
        memory_changes.apply(
            cur,
            second_proposal["change_id"],
            version=1,
            request_id=uuid4(),
            approval=_approval("Replace the old rule"),
            actor="agent",
        )
    assert memories.get_memory(cur, second_old["memory_id"])["status"] == "active"
    assert (
        nominations._require_pending(cur, second_successor["nomination_id"])["status"] == "pending"
    )
    assert memory_changes.get(cur, second_proposal["change_id"])["status"] == "pending"


def test_replace_rejects_a_successor_changed_after_proposal(cur):
    old = _memory(cur, f"old adapter rule {uuid4()}")
    successor_content = f"verify the export digest before sending {uuid4()}"
    revised_content = f"verify the transport envelope before sending {uuid4()}"
    evidence_id = _evidence(cur, successor_content)
    successor = nominations.create_nomination(
        cur,
        content=successor_content,
        kind="user_explicit",
        evidence=[evidence_id],
        actor="agent",
    )
    target = memories.memory_details(cur, old["memory_id"])
    evidence = [
        {"kind": "ledger", "id": str(evidence_id), "observation": "the export needs a digest"}
    ]
    proposal = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=target["current_revision_id"],
        target_updated_at=target["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the transport envelope rule replaces the adapter rule",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
        evidence=evidence,
        actor="agent",
    )
    revised = nominations.revise(
        cur,
        successor["nomination_id"],
        content=revised_content,
        actor="user",
    )
    with pytest.raises(MashuError, match="successor nomination version changed"):
        memory_changes.propose(
            cur,
            target_memory_id=old["memory_id"],
            target_revision_id=target["current_revision_id"],
            target_updated_at=target["updated_at"],
            operation="replace",
            retirement_kind="superseded",
            retire_reason="the stale candidate must not become a proposal",
            successor_nomination_id=successor["nomination_id"],
            successor_nomination_version=successor["version"],
            evidence=evidence,
            actor="agent",
        )

    with pytest.raises(MashuError, match="successor nomination changed"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=proposal["version"],
            request_id=uuid4(),
            approval=_approval("Replace the adapter rule"),
            actor="agent",
        )
    assert memories.get_memory(cur, old["memory_id"])["status"] == "active"
    assert (
        nominations._require_pending(cur, successor["nomination_id"])["content"] == revised_content
    )

    refreshed = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=target["current_revision_id"],
        target_updated_at=target["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the revised transport envelope rule replaces the adapter rule",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=revised["version"],
        evidence=evidence,
        actor="agent",
        change_id=proposal["change_id"],
    )
    assert refreshed["version"] == proposal["version"] + 1
    assert refreshed["successor_snapshot"]["content"] == revised_content
    applied = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=refreshed["version"],
        request_id=uuid4(),
        approval=_approval("Replace it with the revised transport envelope rule"),
        actor="agent",
    )
    assert applied["memory"]["content"] == revised_content


def test_replace_inherits_guard_settings_and_clears_nomination_scope(cur):
    scope = scopes.create_scope(cur, name=f"candidate-only scope {uuid4()}", actor="user")
    old = memories.remember(
        cur,
        content=f"run the deployment checksum guard {uuid4()}",
        actor="user",
        delivery="guard",
        guard_action="deploy",
    )
    successor_content = f"verify the release signature before deployment {uuid4()}"
    evidence_id = _evidence(cur, successor_content)
    successor = nominations.create_nomination(
        cur,
        content=successor_content,
        kind="incident",
        evidence=[evidence_id],
        actor="agent",
        scope_id=scope["scope_id"],
    )
    target = memories.memory_details(cur, old["memory_id"])
    proposal = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=target["current_revision_id"],
        target_updated_at=target["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the release signature check replaces the deployment checksum guard",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
        evidence=[
            {
                "kind": "ledger",
                "id": str(evidence_id),
                "observation": "the release signature check prevents this deployment failure",
            }
        ],
        actor="agent",
    )
    assert proposal["successor_delivery"] == "guard"
    assert proposal["successor_scope_id"] is None
    assert proposal["successor_guard_action"] == "deploy"

    applied = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=proposal["version"],
        request_id=uuid4(),
        approval=_approval("Replace the deployment guard with the release signature check"),
        actor="agent",
    )
    assert applied["memory"]["delivery"] == "guard"
    assert applied["memory"]["guard_action"] == "deploy"
    assert applied["memory"]["scope_id"] is None
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'memory_change_applied' "
        "AND memory_id = %s ORDER BY event_id DESC LIMIT 1",
        (old["memory_id"],),
    )
    assert cur.fetchone()["detail"]["successor_settings"] == {
        "delivery": "guard",
        "scope_id": None,
        "guard_action": "deploy",
    }


def test_changed_successor_delivery_is_part_of_the_proposal_version(cur):
    old_scope = scopes.create_scope(cur, name=f"old delivery scope {uuid4()}", actor="user")
    new_scope = scopes.create_scope(cur, name=f"new delivery scope {uuid4()}", actor="user")
    old = memories.remember(
        cur,
        content=f"keep the legacy importer scoped {uuid4()}",
        actor="user",
        delivery="scope",
        scope_id=old_scope["scope_id"],
    )
    successor_content = f"validate the signed import manifest {uuid4()}"
    evidence_id = _evidence(cur, successor_content)
    successor = nominations.create_nomination(
        cur,
        content=successor_content,
        kind="user_explicit",
        evidence=[evidence_id],
        actor="agent",
    )
    target = memories.memory_details(cur, old["memory_id"])
    evidence = [
        {"kind": "ledger", "id": str(evidence_id), "observation": "the importer needs a signature"}
    ]
    proposal = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=target["current_revision_id"],
        target_updated_at=target["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the signed manifest rule replaces the legacy importer rule",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
        evidence=evidence,
        actor="agent",
    )
    changed = memory_changes.propose(
        cur,
        target_memory_id=old["memory_id"],
        target_revision_id=target["current_revision_id"],
        target_updated_at=target["updated_at"],
        operation="replace",
        retirement_kind="superseded",
        retire_reason="the signed manifest rule replaces the legacy importer rule",
        successor_nomination_id=successor["nomination_id"],
        successor_nomination_version=successor["version"],
        successor_settings={
            "delivery": "scope",
            "scope_id": new_scope["scope_id"],
            "guard_action": None,
        },
        evidence=evidence,
        actor="agent",
        change_id=proposal["change_id"],
    )
    assert changed["version"] == proposal["version"] + 1
    with pytest.raises(MashuError, match="version changed"):
        memory_changes.apply(
            cur,
            proposal["change_id"],
            version=proposal["version"],
            request_id=uuid4(),
            approval=_approval("Replace it and move delivery to the new scope"),
            actor="agent",
        )

    applied = memory_changes.apply(
        cur,
        proposal["change_id"],
        version=changed["version"],
        request_id=uuid4(),
        approval=_approval("Replace it and move delivery to the new scope"),
        actor="agent",
    )
    assert applied["memory"]["delivery"] == "scope"
    assert applied["memory"]["scope_id"] == str(new_scope["scope_id"])
