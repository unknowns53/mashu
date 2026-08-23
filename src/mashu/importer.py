"""Moving what the native memory files hold into Mashu (specification 27.1).

Two rules govern this and both come from what the existing store turned out to
contain.

Everything arrives as a candidate and goes through review. The files are
summary prose in which observation and interpretation have fused, and an audit
of them found unreviewed interpretation already sitting there as though it were
established. Importing without review would make that the opening stock of the
system built to prevent it, so the auto commit line is held shut here even for
the types that would otherwise pass it.

Every version records where it came from. Without the reference an imported
claim is indistinguishable from one the system observed, and the question that
gets asked of a doubtful memory later is which file said so.

The unit of the migration is the scope, not the file. Migration is rebuilding a
state, not replaying a history: a scope counts as migrated once its current
state, its active preferences and its main decisions stand up, and the rest of
the files can be pulled across if and when they are wanted. Making the whole
inventory a precondition makes the cost scale with the pile and the migration
fail. Whether a scope has got there is asked of mashu.scopes, which reads the
same three types against what each scope declared it needs.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import proposals, resolution
from mashu.errors import MashuError
from mashu.models import MemoryType, ProposalOperation, SourceType

HOLD_REASON = "imported from the native memory store (27.1); review before it is trusted"

REQUIRED_FIELDS = ("type", "title", "content", "source_reference")


class ImportError_(MashuError):
    """An item cannot be imported as given."""


def import_items(
    cur: psycopg.Cursor,
    *,
    scope_id: UUID,
    items: list[dict[str, Any]],
    actor: str,
    session_id: UUID | None = None,
) -> dict[str, Any]:
    """Propose each item as a candidate in one scope.

    An item whose title matches an existing entity closely enough is not
    created as a rival: it becomes a version on the entity it resembles, which
    is what the migration usually means when the same subject appears in
    several files. Only when the agent has no entity to attach to does a
    provisional one get made.

    Returns what happened per item rather than raising on the first problem. A
    migration run over dozens of items should report the ones it could not
    place, not stop at them.
    """
    imported: list[dict[str, Any]] = []
    attached: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        missing = [f for f in REQUIRED_FIELDS if not item.get(f)]
        if missing:
            failed.append({"index": index, "title": item.get("title"), "missing": missing})
            continue

        try:
            similar = resolution.find_similar(cur, scope_id=scope_id, title=item["title"], limit=1)
            if similar:
                target = similar[0]
                result = _attach(cur, target, item, scope_id, actor, session_id)
                attached.append(
                    {
                        "index": index,
                        "title": item["title"],
                        "onto": target["title"],
                        "similarity": round(target["similarity"], 3),
                        "proposal_id": result["proposal"]["proposal_id"],
                    }
                )
            else:
                result = _create(cur, item, scope_id, actor, session_id)
                imported.append(
                    {
                        "index": index,
                        "title": item["title"],
                        "proposal_id": result["proposal"]["proposal_id"],
                    }
                )
        except MashuError as error:
            failed.append({"index": index, "title": item["title"], "error": str(error)})

    return {
        "scope_id": scope_id,
        "created": imported,
        "attached": attached,
        "failed": failed,
    }


def _payload(item: dict[str, Any], scope_id: UUID) -> dict[str, Any]:
    return {
        "scope_id": str(scope_id),
        "type": str(MemoryType(item["type"])),
        "title": item["title"],
        "content": item["content"],
        "source_type": str(SourceType(item.get("source_type", SourceType.FILE))),
        "source_reference": item["source_reference"],
        # The short standing form, when the source file marked one off. It is
        # the file's own opening claim, taken as written, not a summary made
        # here: a summary would be an interpretation entering the store with
        # the migration rather than through review (21.2).
        "directive": item.get("directive"),
    }


def _create(cur, item, scope_id, actor, session_id):
    return proposals.propose(
        cur,
        actor=actor,
        operation=ProposalOperation.CREATE,
        payload=_payload(item, scope_id),
        session_id=session_id,
        allow_similar=True,
        allow_duplicate=True,
        hold_for_review=HOLD_REASON,
    )


def _attach(cur, target, item, scope_id, actor, session_id):
    from mashu import store

    entity = store.get_entity(cur, target["memory_id"])
    payload = _payload(item, scope_id)
    payload.pop("scope_id")
    payload.pop("type")
    return proposals.propose(
        cur,
        actor=actor,
        operation=ProposalOperation.UPDATE_VERSION,
        target_memory=entity["memory_id"],
        based_on_version=entity["latest_version"],
        payload=payload,
        session_id=session_id,
        allow_duplicate=True,
        hold_for_review=HOLD_REASON,
    )


# --------------------------------------------------------------------------
# stocktaking
