"""Validate and retain the stated source of a Memory decision."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from mashu import redact
from mashu.errors import MashuError, RefusedError


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
) -> dict[str, Any]:
    """Validate the caller's stated authorization without claiming to authenticate it."""
    kind = approval.get("kind")
    if kind not in ("user_direct", "user_instruction", "policy"):
        raise MashuError("approval kind must be user_direct, user_instruction, or policy")
    if kind == "policy":
        raise MashuError("no server-side Memory change condition is registered for policy approval")

    source: dict[str, Any] = {"kind": kind}
    if kind == "user_instruction":
        instruction = _safe_text(approval.get("instruction"), "instruction", required=True)
        source["instruction"] = instruction
        reference = _safe_text(approval.get("conversation_ref"), "conversation reference")
        if reference:
            source["conversation_ref"] = reference

    expected = {str(value) for value in required_conflicts or []}
    try:
        supplied = {str(UUID(str(value))) for value in (approval.get("conflict_ids") or [])}
    except (ValueError, TypeError, AttributeError) as error:
        raise MashuError("conflict_ids must contain Memory UUIDs") from error
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
    verdict = redact.check(text)
    if not verdict.allowed:
        raise RefusedError(verdict.reason())
    return text
