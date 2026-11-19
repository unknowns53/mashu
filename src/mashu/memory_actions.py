"""Human Memory and Memory change operations shared by the terminal screens and desktop app."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import psycopg

from mashu import memories, memory_changes, nominations, projects, temporary, topics
from mashu.errors import ChangedSinceReadError, MashuError

#: The kind a person's own retirement records. superseded and relocated are recorded by the
#: operations that link a successor or a Temporary Context, never chosen.
RETIREMENT_KIND = "withdrawn"

#: Conflicts that must be read, and named in the approval, before a change is applied.
BLOCKING_KINDS = ("withdrawn", "legacy")


def project_id_named(cur: psycopg.Cursor, name: str | None) -> UUID | None:
    """The named project's id, or None for no project."""
    return projects.require_project(cur, name)["project_id"] if name else None


def project_ids_named(
    cur: psycopg.Cursor, names: Sequence[str], *, home: UUID | None = None
) -> tuple[UUID | None, list[UUID]]:
    """The home project and the other projects of a rule delivered to the named projects.

    The current home project stays home while it is named; otherwise the first named one is.
    """
    ids = list(
        dict.fromkeys(projects.require_project(cur, name)["project_id"] for name in names if name)
    )
    if not ids:
        return None, []
    first = home if home in ids else ids[0]
    return first, [project_id for project_id in ids if project_id != first]


def topic_id_for(cur: psycopg.Cursor, choice: dict[str, Any] | None, *, actor: str) -> UUID | None:
    """The chosen topic's id, creating a described topic inside the caller's write.

    A choice is {"topic_id": id}, {"name": an open topic's name}, or a new topic described
    by name, trigger, and optionally project_name.
    """
    if choice is None:
        return None
    if choice.get("topic_id") is not None:
        return choice["topic_id"]
    if choice.get("trigger") is None:
        return topics.require_topic(cur, choice["name"])["topic_id"]
    return topics.create_topic(
        cur,
        name=choice["name"],
        trigger=choice["trigger"],
        project_id=project_id_named(cur, choice.get("project_name")),
        actor=actor,
    )["topic_id"]


def remember(
    cur: psycopg.Cursor,
    *,
    content: str,
    why: str | None,
    delivery: str,
    project_name: str | None,
    topic: dict[str, Any] | None,
    acknowledged: list[UUID] | None,
    actor: str,
    project_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Write a rule the person states directly; `project_names` delivers a project rule to each.

    Raises RetiredConflictError naming the retirements it repeats until acknowledged lists
    exactly those, so the person reads why they were retired before writing it back.
    """
    home, others = project_ids_named(cur, project_names) if project_names else (None, [])
    return memories.remember(
        cur,
        content=content,
        actor=actor,
        project_id=home if project_names else project_id_named(cur, project_name),
        delivery=delivery,
        topic_id=topic_id_for(cur, topic, actor=actor),
        acknowledged_conflicts=acknowledged,
        why=why,
        also_project_ids=others,
    )


def set_delivery(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    delivery: str,
    project_name: str | None,
    topic: dict[str, Any] | None,
    actor: str,
    project_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Move a rule to another delivery; no project named drops the one it had.

    `project_names` is every project a project rule reaches, so one left out stops receiving it.
    """
    others: list[UUID] | None = None
    if project_names is not None and delivery == "project":
        current = memories.get_memory(cur, memory_id)
        project_id, others = project_ids_named(
            cur, project_names, home=current["project_id"] if current else None
        )
    else:
        project_id = project_id_named(cur, project_name)
    return memories.set_delivery(
        cur,
        memory_id,
        delivery=delivery,
        actor=actor,
        project_id=project_id,
        clear_project=project_id is None,
        topic_id=topic_id_for(cur, topic, actor=actor),
        approval_source={"kind": "user_direct"},
        also_project_ids=others,
    )


def restore(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    reason: str,
    acknowledged: list[UUID] | None,
    actor: str,
    delivery: str | None = None,
    project_name: str | None = None,
    project_names: Sequence[str] | None = None,
    topic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bring a retired rule back on the person's word, optionally into a new delivery.

    The person reverses the retirement directly, so the reason they give is the reversal;
    the former kind and reason stay on the restore event. Retired rules the body repeats are
    named back with RetiredConflictError until they are acknowledged, as remember does.
    """
    approval: dict[str, Any] = {"kind": "user_direct"}
    if acknowledged:
        approval["conflict_ids"] = sorted(str(ident) for ident in acknowledged)
    if delivery is None:
        return memories.restore(
            cur, memory_id, reason=reason, actor=actor, approval_source=approval
        )
    others: list[UUID] | None = None
    if project_names is not None and delivery == "project":
        current = memories.get_memory(cur, memory_id)
        project_id, others = project_ids_named(
            cur, project_names, home=current["project_id"] if current else None
        )
    else:
        project_id = project_id_named(cur, project_name)
    return memories.restore(
        cur,
        memory_id,
        reason=reason,
        actor=actor,
        approval_source=approval,
        delivery=delivery,
        project_id=project_id,
        clear_project=project_id is None,
        topic_id=topic_id_for(cur, topic, actor=actor),
        also_project_ids=others,
    )


def edit_memory(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    content: str,
    acknowledged: list[UUID] | None,
    actor: str,
    delivery: str | None = None,
    project_name: str | None = None,
    project_names: Sequence[str] | None = None,
    topic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rewrite an active rule's body and optionally its delivery in one transaction.

    Runs whichever of body revision or delivery move frees capacity first when both change.
    """
    current = memories.memory_details(cur, memory_id)
    if current is None:
        raise MashuError(f"no memory {memory_id}")
    if current["status"] != "active":
        raise MashuError(f"memory {memory_id} is {current['status']}, and only active rows change")

    body_changed = content != current["content"]
    delivery_changed = False
    if delivery is not None:
        if delivery != current["delivery"]:
            delivery_changed = True
        elif delivery == "project":
            wanted = (
                list(dict.fromkeys(project_names))
                if project_names is not None
                else ([project_name] if project_name else [])
            )
            delivery_changed = set(wanted) != set(current["project_names"] or [])
        elif delivery == "topic":
            wanted_topic_id = topic.get("topic_id") if topic else None
            delivery_changed = topic is None or wanted_topic_id != current["topic_id"]

    if not body_changed and not delivery_changed:
        return current

    def do_revise() -> dict[str, Any]:
        return memories.revise(
            cur,
            memory_id,
            content=content,
            actor=actor,
            acknowledged_conflicts=acknowledged,
        )

    def do_deliver() -> dict[str, Any]:
        assert delivery is not None
        return set_delivery(
            cur,
            memory_id,
            delivery=delivery,
            project_name=project_name,
            topic=topic,
            actor=actor,
            project_names=project_names if delivery == "project" else None,
        )

    if body_changed and delivery_changed:
        if len(content) <= len(current["content"]):
            do_revise()
            return do_deliver()
        do_deliver()
        return do_revise()
    if body_changed:
        return do_revise()
    return do_deliver()


def retire(cur: psycopg.Cursor, memory_id: UUID, *, reason: str, actor: str) -> dict[str, Any]:
    """Retire a rule on the person's word, with a reason and no kind to choose.

    A similar rule offered later is shown this reason and waits for confirmation. A move to
    Temporary Context is the conversion's own step, which records it as relocated.
    """
    return memories.retire(
        cur,
        memory_id,
        reason=reason,
        actor=actor,
        retirement_kind=RETIREMENT_KIND,
        approval_source={"kind": "user_direct"},
    )


def add_temporary(
    cur: psycopg.Cursor,
    *,
    content: str,
    days: float,
    actor: str,
    project_name: str | None = None,
    project_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Record a temporary context for every project, or for the projects named."""
    if project_names:
        home, others = project_ids_named(cur, project_names)
    else:
        home, others = project_id_named(cur, project_name), []
    return temporary.put_temporary(
        cur, content=content, actor=actor, days=days, project_id=home, also_project_ids=others
    )


def revise_temporary(
    cur: psycopg.Cursor,
    context_id: UUID,
    *,
    content: str,
    project_name: str | None,
    actor: str,
    project_names: Sequence[str] | None = None,
    days: float | None = None,
) -> dict[str, Any]:
    """Rewrite a standing temporary context's text, projects, and optionally its expiry."""
    others: list[UUID] | None = None
    if project_names is not None:
        cur.execute("SELECT project_id FROM temporary_context WHERE context_id = %s", (context_id,))
        existing = cur.fetchone()
        home_before = existing["project_id"] if existing else None
        project_id, others = project_ids_named(cur, project_names, home=home_before)
    else:
        project_id = project_id_named(cur, project_name)
    return temporary.revise(
        cur,
        context_id,
        content=content,
        project_id=project_id,
        actor=actor,
        also_project_ids=others,
        days=days,
    )


def convert_to_temporary(
    cur: psycopg.Cursor,
    memory_id: UUID,
    *,
    days: float,
    actor: str,
    project_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Convert an active memory to temporary context, keeping or overriding its projects."""
    if project_names is None:
        return memories.convert_to_temporary(cur, memory_id, days=days, actor=actor)
    home, others = project_ids_named(cur, project_names)
    return memories.convert_to_temporary(
        cur,
        memory_id,
        days=days,
        actor=actor,
        project_id=home,
        also_project_ids=others,
        override_projects=True,
    )


def convert_temporary_to_memory(
    cur: psycopg.Cursor,
    context_id: UUID,
    *,
    actor: str,
    acknowledged: list[UUID] | None = None,
    delivery: str | None = None,
    project_name: str | None = None,
    project_names: Sequence[str] | None = None,
    topic: dict[str, Any] | None = None,
    why: str | None = None,
) -> dict[str, Any]:
    """Convert a temporary context to an active memory, keeping or choosing its delivery."""
    if delivery is None:
        return temporary.convert_to_memory(
            cur, context_id, actor=actor, acknowledged_conflicts=acknowledged, why=why
        )
    if delivery == "project":
        if project_names is not None:
            home, others = project_ids_named(cur, project_names)
        else:
            home, others = project_id_named(cur, project_name), []
    else:
        home, others = None, []
    return temporary.convert_to_memory(
        cur,
        context_id,
        actor=actor,
        acknowledged_conflicts=acknowledged,
        delivery=delivery,
        project_id=home,
        also_project_ids=others,
        topic_id=topic_id_for(cur, topic, actor=actor),
        why=why,
    )


def pending_change(cur: psycopg.Cursor, change_id: UUID) -> dict[str, Any]:
    """One pending Memory change as the review screens show it."""
    found = next(
        (row for row in memory_changes.pending(cur) if row["change_id"] == change_id), None
    )
    if found is None:
        raise MashuError("memory change is no longer pending")
    return found


def blocking_conflict_ids(change: dict[str, Any]) -> list[UUID]:
    """The retired conflicts a person must read before applying this change."""
    return [
        item["memory_id"]
        for item in change.get("conflicts", [])
        if item.get("retirement_kind") in BLOCKING_KINDS
    ]


def apply_change(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    version: int,
    conflict_ids: list[UUID],
    actor: str,
) -> dict[str, Any]:
    """Apply exactly the version shown, approving the conflicts the person read.

    Raises ChangedSinceReadError when the change, its target, its successor, or its
    conflicts moved since; refresh_change then re-proposes it against what is current.
    """
    return memory_changes.apply(
        cur,
        change_id,
        version=version,
        request_id=uuid4(),
        approval={"kind": "user_direct", "conflict_ids": conflict_ids},
        actor=actor,
    )


def _repropose(
    cur: psycopg.Cursor,
    current: dict[str, Any],
    *,
    target: dict[str, Any],
    successor_version: int | None,
    actor: str,
    retire_reason: str | None,
    restore_reason: str | None,
    successor_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return memory_changes.propose(
        cur,
        target_memory_id=current["target_memory_id"],
        target_revision_id=target.get("current_revision_id"),
        target_updated_at=target["updated_at"],
        operation=current["operation"],
        evidence=current["evidence"],
        actor=actor,
        retirement_kind=current["retirement_kind"],
        retire_reason=retire_reason,
        successor_nomination_id=current["successor_nomination_id"],
        successor_nomination_version=successor_version,
        successor_settings=(
            memory_changes.requested_settings(current)
            if successor_settings is None
            else successor_settings
        ),
        relocated_to_kind=current["relocated_to_kind"],
        relocated_to_id=current["relocated_to_id"],
        restore_reason=restore_reason,
        change_id=current["change_id"],
    )


def refresh_change(cur: psycopg.Cursor, change_id: UUID, *, actor: str) -> dict[str, Any]:
    """Re-propose a change against its current target and conflicts after a stale refusal."""
    current = memory_changes.get(cur, change_id)
    if current is None or current["status"] != "pending":
        raise MashuError("memory change is no longer pending")
    target = memories.memory_details(cur, current["target_memory_id"])
    if target is None:
        raise MashuError("target Memory no longer exists")
    successor_version = None
    if current["operation"] == "replace":
        cur.execute(
            "SELECT * FROM nomination WHERE nomination_id = %s FOR UPDATE",
            (current["successor_nomination_id"],),
        )
        successor = cur.fetchone()
        if successor is None or successor["status"] != "pending":
            raise MashuError("replacement nomination is no longer pending")
        refreshed = nominations.refresh_conflicts(
            cur,
            current["successor_nomination_id"],
            nominations.current_conflict_ids(cur, successor["content"]),
            actor=actor,
        )
        if refreshed is None:
            raise MashuError("replacement nomination is no longer pending")
        successor_version = refreshed["version"]
    return _repropose(
        cur,
        current,
        target=target,
        successor_version=successor_version,
        actor=actor,
        retire_reason=current["retire_reason"],
        restore_reason=current["restore_reason"],
    )


def edit_change_reason(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    version: int,
    target_revision_id: UUID | None,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    """Reword a pending change's reason, as long as it and its target are as shown."""
    shown = pending_change(cur, change_id)
    if shown["operation"] == "redeliver":
        raise MashuError("a redeliver proposal has no reason to edit; its evidence says why")
    if not reason.strip():
        raise MashuError("the reason cannot be empty")
    if (
        shown["version"] != version
        or shown["target"].get("current_revision_id") != target_revision_id
    ):
        raise ChangedSinceReadError("the proposal or its target changed; read it again first")
    restoring = shown["operation"] == "restore"
    return _repropose(
        cur,
        shown,
        target=shown["target"],
        successor_version=shown["successor"]["version"]
        if shown["operation"] == "replace"
        else None,
        actor=actor,
        retire_reason=shown["retire_reason"] if restoring else reason,
        restore_reason=reason if restoring else shown["restore_reason"],
    )


def _change_settings(
    cur: psycopg.Cursor,
    *,
    delivery: str,
    project_name: str | None,
    topic: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build memory_changes.propose's successor_settings from human form fields."""
    if delivery == "always":
        return {"delivery": "always", "project_id": None}
    if delivery == "project":
        return {"delivery": "project", "project_id": project_id_named(cur, project_name)}
    if delivery == "topic":
        if topic is None:
            raise MashuError("topic delivery needs a topic")
        if topic.get("topic_id") is not None:
            row = topics.lock_open_topic(cur, topic["topic_id"])
            return {"delivery": "topic", "project_id": row["project_id"], "topic": row["name"]}
        if topic.get("trigger") is not None:
            return {
                "delivery": "topic",
                "project_id": project_id_named(cur, topic.get("project_name")),
                "topic": topic["name"],
                "topic_trigger": topic["trigger"],
            }
        row = topics.require_topic(cur, topic["name"])
        return {"delivery": "topic", "project_id": row["project_id"], "topic": row["name"]}
    raise MashuError(f"unknown delivery '{delivery}'")


def edit_change(
    cur: psycopg.Cursor,
    change_id: UUID,
    *,
    version: int,
    target_revision_id: UUID | None,
    actor: str,
    reason: str | None = None,
    successor_content: str | None = None,
    delivery: str | None = None,
    project_name: str | None = None,
    topic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Edit a pending Memory change's reason, replacement wording, or delivery in one step."""
    shown = pending_change(cur, change_id)
    if (
        shown["version"] != version
        or shown["target"].get("current_revision_id") != target_revision_id
    ):
        raise ChangedSinceReadError("the proposal or its target changed; read it again first")

    op = shown["operation"]
    retire_reason = shown["retire_reason"]
    restore_reason = shown["restore_reason"]
    if reason is not None:
        if op == "redeliver":
            raise MashuError("a redeliver proposal has no reason to edit; its evidence says why")
        if not reason.strip():
            raise MashuError("the reason cannot be empty")
        if op == "restore":
            restore_reason = reason
        else:
            retire_reason = reason

    successor_version = shown["successor"]["version"] if op == "replace" else None
    if successor_content is not None:
        if op != "replace":
            raise MashuError("only a replace proposal has replacement wording to edit")
        revised = nominations.revise(
            cur, shown["successor_nomination_id"], content=successor_content, actor=actor
        )
        successor_version = revised["version"]

    settings_arg: dict[str, Any] | None = None
    if delivery is not None:
        if op not in ("redeliver", "replace"):
            raise MashuError(f"a {op} proposal has no delivery to edit")
        settings_arg = _change_settings(
            cur, delivery=delivery, project_name=project_name, topic=topic
        )

    return _repropose(
        cur,
        shown,
        target=shown["target"],
        successor_version=successor_version,
        actor=actor,
        retire_reason=retire_reason,
        restore_reason=restore_reason,
        successor_settings=settings_arg,
    )
