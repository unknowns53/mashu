"""Validate and retain the stated source of a Memory decision."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from mashu import redact
from mashu.errors import MashuError


def json_value(value: Any) -> Any:
    """Make identifiers and timestamps safe for durable JSON request snapshots."""
    if isinstance(value, (UUID, date, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    return value


def validate(
    approval: dict[str, Any],
    *,
    required_conflicts: list[UUID] | None = None,
    reversal_required: bool = False,
    gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the caller's stated authorization without claiming to authenticate it."""
    kind = approval.get("kind")
    if kind not in ("user_direct", "user_instruction", "policy"):
        raise MashuError("approval kind must be user_direct, user_instruction, or policy")
    if kind == "policy":
        raise MashuError("no server-side Memory change condition is registered for policy approval")

    verdict = redact.gate(_snapshot_strings(json_value(approval)))
    if gate is not None:
        if verdict.unchecked:
            gate["unchecked"] = True
        if verdict.malformed:
            gate["malformed"] = verdict.malformed

    source: dict[str, Any] = {"kind": kind}
    if kind == "user_instruction":
        instruction = _safe_text(approval.get("instruction"), "instruction", required=True)
        source["instruction"] = instruction
        reference = _safe_text(approval.get("conversation_ref"), "conversation reference")
        if reference:
            source["conversation_ref"] = reference

    expected = {str(value) for value in required_conflicts or []}
    supplied = _supplied_conflicts(approval)
    if expected != supplied:
        if expected or supplied:
            raise MashuError(
                "retirement conflicts changed or were not explicitly acknowledged; read the "
                "current conflicts and submit their IDs"
            )
    if expected:
        source["conflict_ids"] = sorted(expected)
        if kind == "user_instruction":
            acknowledgment = _safe_text(
                approval.get("conflict_instruction"), "conflict acknowledgment", required=True
            )
            source["conflict_instruction"] = acknowledgment
        else:
            source["conflict_acknowledged_by_action"] = True

    if reversal_required:
        if kind == "user_instruction":
            reversal = _safe_text(
                approval.get("reversal_instruction"), "reversal instruction", required=True
            )
            source["reversal_instruction"] = reversal
        else:
            source["reversal_approved_by_action"] = True

    return source


#: The keys an approval is sent with. Any other key is labelled by position, because its own
#: text may be what the gate refuses.
_APPROVAL_KEYS = frozenset(
    {
        "kind",
        "instruction",
        "conversation_ref",
        "conflict_ids",
        "conflict_instruction",
        "reversal_instruction",
    }
)


def _snapshot_strings(value: Any, path: str | None = None) -> dict[str, str]:
    """Every string in the snapshot, labelled by where it sits in the approval."""
    base = path or "approval"
    if isinstance(value, str):
        return {base: value}
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            out.update(_snapshot_strings(key, f"{base} key #{index}"))
            known = path is None and key in _APPROVAL_KEYS
            out.update(_snapshot_strings(item, key if known else f"{base}.#{index}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(_snapshot_strings(item, f"{base}[{index}]"))
    return out


def acknowledges_conflicts(approval: dict[str, Any], required_conflicts: list[UUID]) -> bool:
    """Whether the approval names exactly these conflicts and, for an instruction, addresses them.

    Content checks on the acknowledgment text stay in validate, so a text that is present
    but refused still fails there rather than reading as unacknowledged.
    """
    expected = {str(value) for value in required_conflicts}
    if _supplied_conflicts(approval) != expected:
        return False
    if expected and approval.get("kind") == "user_instruction":
        text = approval.get("conflict_instruction")
        return isinstance(text, str) and bool(text.strip())
    return True


def _supplied_conflicts(approval: dict[str, Any]) -> set[str]:
    try:
        return {str(UUID(str(value))) for value in (approval.get("conflict_ids") or [])}
    except (ValueError, TypeError, AttributeError) as error:
        raise MashuError("conflict_ids must contain Memory UUIDs") from error


def _safe_text(value: Any, label: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise MashuError(f"{label} is required")
        return None
    if not isinstance(value, str):
        raise MashuError(f"{label} must be text")
    text = value.strip()
    if not text:
        if required:
            raise MashuError(f"{label} is required")
        return None
    if len(text) > 1000:
        raise MashuError(f"{label} is too long (maximum 1000 characters)")
    redact.gate({label: text})
    return text
