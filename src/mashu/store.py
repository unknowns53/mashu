"""The state changes the system is allowed to make on its own.

Everything a caller can reach from here is deterministic: it creates versions,
moves statuses along the transition table, moves the active pointer, and writes
the matching event rows. None of it decides whether a memory is true. That
judgement enters through a proposal and, for anything but an auto commit, a
human review.

Every function takes a cursor. The caller owns the transaction, which is how a
pointer switch and its status change stay atomic (specification 26).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from mashu import events
from mashu.embed import get_embedder
from mashu.errors import (
    ConcurrentUpdateError,
    DeliveryError,
    MashuError,
    MergeError,
    NotFoundError,
)
from mashu.models import (
    Delivery,
    EntityStatus,
    EventType,
    MemoryType,
    SourceType,
    VersionStatus,
)
from mashu.transitions import check_can_be_active, check_transition


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def get_entity(cur: psycopg.Cursor, memory_id: UUID, *, lock: bool = False) -> dict:
    """One entity row. With lock, hold it for the rest of the transaction.

    The lock is what stops two concurrent adoptions from each reading the same
    latest_version and both passing the optimistic check.
    """
    cur.execute(
        "SELECT * FROM memory_entity WHERE memory_id = %s" + (" FOR UPDATE" if lock else ""),
        (memory_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no memory entity {memory_id}")
    return row


def get_version(cur: psycopg.Cursor, version_id: UUID) -> dict:
    """One version row."""
    cur.execute("SELECT * FROM memory_version WHERE version_id = %s", (version_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"no memory version {version_id}")
    return row


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------
def create_scope(
    cur: psycopg.Cursor,
    *,
    name: str,
    actor: str,
    description: str | None = None,
) -> UUID:
    """Add a scope to the ledger. Only the user does this (specification 7).

    The name and description are embedded here rather than at query time.
    Scope detection compares every query against all of them, and a ledger
    that rarely changes should not be re-encoded on every search.
    """
    cur.execute(
        """
        INSERT INTO scope (name, description, name_embedding)
        VALUES (%s, %s, %s) RETURNING scope_id
        """,
        (name, description, embed_text(scope_description(name, description))),
    )
    scope_id = cur.fetchone()["scope_id"]
    events.record(cur, EventType.SCOPE_CREATED, actor, detail={"name": name})
    return scope_id


def create_entity(
    cur: psycopg.Cursor,
    *,
    scope_id: UUID,
    type: MemoryType,
    title: str,
    content: str,
    source_type: SourceType,
    created_by: str,
    actor: str,
    adopt: bool,
    source_reference: str | None = None,
    status: VersionStatus = VersionStatus.CANDIDATE,
    entity_status: EntityStatus = EntityStatus.ACTIVE,
    reason: str | None = None,
    directive: str | None = None,
    evidence: list[UUID] | None = None,
) -> tuple[UUID, UUID]:
    """Create an entity together with its first version.

    With adopt, the version becomes the active one straight away, which is what
    an auto commit does. Without it the version sits as a candidate the pointer
    does not target, waiting for review (specification 17).

    entity_status is provisional when the title looked like an existing entity
    and the agent chose to create anyway. The entity works normally except that
    retrieval keeps it out of layer 1 until the review settles it
    (specification 20.1).
    """
    # Section 14 gives a scope one current state, so scope_required keeps the
    # count bounded by the number of scopes rather than by the size of the
    # store. Everything else starts pull_only and has to be promoted on
    # purpose: that is what stops the session opening from growing with the
    # inventory (21.2).
    delivery = (
        Delivery.SCOPE_REQUIRED if MemoryType(type) is MemoryType.STATE else Delivery.PULL_ONLY
    )
    cur.execute(
        """
        INSERT INTO memory_entity (scope_id, type, title, status, delivery, title_embedding)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING memory_id
        """,
        (
            scope_id,
            str(MemoryType(type)),
            title,
            str(EntityStatus(entity_status)),
            str(delivery),
            embed_text(title),
        ),
    )
    memory_id = cur.fetchone()["memory_id"]
    events.record(
        cur,
        EventType.ENTITY_CREATED,
        actor,
        memory_id=memory_id,
        detail={
            "title": title,
            "type": str(MemoryType(type)),
            "entity_status": str(EntityStatus(entity_status)),
        },
    )

    version_id = _insert_version(
        cur,
        memory_id=memory_id,
        content=content,
        status=status,
        supersedes=None,
        reason=reason,
        source_type=source_type,
        source_reference=source_reference,
        created_by=created_by,
        actor=actor,
        directive=directive,
    )
    if evidence:
        record_evidence(cur, from_version=version_id, to_memory=evidence, actor=actor)
    _set_latest(cur, memory_id, version_id)
    if adopt:
        _point_active_at(cur, memory_id=memory_id, version_id=version_id, actor=actor)
    return memory_id, version_id


def add_version(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    content: str,
    source_type: SourceType,
    created_by: str,
    actor: str,
    based_on_version: UUID | None,
    adopt: bool,
    source_reference: str | None = None,
    status: VersionStatus = VersionStatus.CANDIDATE,
    reason: str | None = None,
    directive: str | None = None,
    evidence: list[UUID] | None = None,
) -> UUID:
    """Add a version to an existing entity.

    based_on_version is the optimistic lock (specification 24): it has to match
    what the entity currently calls latest, otherwise the caller wrote its
    proposal against a state that has since moved and has to read and propose
    again.
    """
    entity = get_entity(cur, memory_id, lock=True)
    if entity["latest_version"] != based_on_version:
        raise ConcurrentUpdateError(
            f"entity {memory_id} is now at {entity['latest_version']}, "
            f"but the change was written against {based_on_version}; "
            f"re-read and propose again"
        )

    previous_active = entity["active_version"]
    version_id = _insert_version(
        cur,
        memory_id=memory_id,
        content=content,
        status=status,
        supersedes=previous_active if adopt else None,
        reason=reason,
        source_type=source_type,
        source_reference=source_reference,
        created_by=created_by,
        actor=actor,
        directive=directive,
    )
    if evidence:
        record_evidence(cur, from_version=version_id, to_memory=evidence, actor=actor)
    _set_latest(cur, memory_id, version_id)
    if adopt:
        _point_active_at(
            cur, memory_id=memory_id, version_id=version_id, actor=actor, reason=reason
        )
    return version_id


def set_status(
    cur: psycopg.Cursor,
    *,
    version_id: UUID,
    target: VersionStatus,
    actor: str,
    reason: str,
) -> None:
    """Move one version along the transition table.

    A reason is required. Specification 31 counts a change whose motive was not
    recorded as an untraceable one.

    The reason is written onto the version as well as into the log, because
    version.reason means the reason the version is in the state it is now
    (specification 26). Layer 3 of retrieval hands that text to the agent as
    the refutation, and the schema refuses a disproven version without one.

    If the version being moved is the active one and the new status cannot be
    active, the pointer is cleared rather than left dangling: the entity then
    has no current truth, which is the honest reading of disproving what was
    being relied on.
    """
    version = get_version(cur, version_id)
    current = VersionStatus(version["status"])
    target = VersionStatus(target)
    check_transition(current, target)

    entity = get_entity(cur, version["memory_id"], lock=True)

    cur.execute(
        "UPDATE memory_version SET status = %s, reason = %s WHERE version_id = %s",
        (str(target), reason, version_id),
    )
    events.record(
        cur,
        EventType.STATUS_CHANGED,
        actor,
        memory_id=version["memory_id"],
        version_id=version_id,
        detail={"from": str(current), "to": str(target), "reason": reason},
    )

    if entity["active_version"] == version_id and target not in _ACTIVE_OK:
        _clear_active(cur, memory_id=version["memory_id"], actor=actor, reason=reason)


def set_active(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    version_id: UUID,
    actor: str,
    reason: str | None = None,
) -> None:
    """Adopt an existing version of this entity as the active one.

    The version that was active becomes superseded in the same transaction, so
    there is never a moment where two versions could both be read as current.
    """
    get_entity(cur, memory_id, lock=True)  # held for the rest of the transaction
    version = get_version(cur, version_id)
    if version["memory_id"] != memory_id:
        raise NotFoundError(
            f"version {version_id} belongs to {version['memory_id']}, not {memory_id}"
        )
    check_can_be_active(VersionStatus(version["status"]))
    _point_active_at(cur, memory_id=memory_id, version_id=version_id, actor=actor, reason=reason)


def record_evidence(
    cur: psycopg.Cursor,
    *,
    from_version: UUID,
    to_memory: list[UUID] | tuple[UUID, ...],
    actor: str,
) -> int:
    """Record what a version rests on (specification 19).

    Only the edges are kept, not a dependency graph. The point is the reverse
    lookup: when a memory turns out to be wrong, the question asked next is
    what was built on it, and that has to be answerable without walking the
    whole store.

    Nothing cascades from an edge. A disproven ground puts its dependants in
    front of a reviewer; it does not retire them, because whether a conclusion
    survives losing one of its grounds is not something the layer can decide.

    A version may not cite its own entity. Section 14 lets a current state's
    summary say only what its references already say, and an entity that is its
    own reference makes that condition vacuous.
    """
    own = get_version(cur, from_version)["memory_id"]
    written = 0
    for memory_id in to_memory:
        if memory_id == own:
            raise MashuError(
                f"version {from_version} cannot rest on its own entity {own}; "
                f"a reference has to point outside the thing it supports"
            )
        get_entity(cur, memory_id)
        cur.execute(
            "INSERT INTO memory_evidence (from_version, to_memory) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (from_version, memory_id),
        )
        written += cur.rowcount

    if written:
        events.record(
            cur,
            EventType.EVIDENCE_RECORDED,
            actor,
            memory_id=own,
            version_id=from_version,
            detail={"to_memory": [str(m) for m in to_memory], "written": written},
        )
    return written


def evidence_for(cur: psycopg.Cursor, version_id: UUID) -> list[dict[str, Any]]:
    """What this version says it rests on, with each ground's current standing."""
    cur.execute(
        """
        SELECT e.memory_id, e.type, e.title, e.status AS entity_status,
               v.status AS version_status
        FROM memory_evidence ev
        JOIN memory_entity e ON e.memory_id = ev.to_memory
        LEFT JOIN memory_version v ON v.version_id = e.active_version
        WHERE ev.from_version = %s
        ORDER BY e.title
        """,
        (version_id,),
    )
    return cur.fetchall()


def resting_on(cur: psycopg.Cursor, memory_id: UUID) -> list[dict[str, Any]]:
    """The reverse lookup of specification 19: what was built on this memory.

    Answers for every citing version, not only the active ones, and says which
    of them an entity currently points at. A retired version that cited this
    memory is not a live concern, and the caller has to be able to tell the two
    apart without a second query.
    """
    cur.execute(
        """
        SELECT e.memory_id, e.type, e.title, v.version_id, v.status,
               v.version_id = e.active_version AS is_active
        FROM memory_evidence ev
        JOIN memory_version v ON v.version_id = ev.from_version
        JOIN memory_entity e ON e.memory_id = v.memory_id
        WHERE ev.to_memory = %s
        ORDER BY is_active DESC, e.title
        """,
        (memory_id,),
    )
    return cur.fetchall()


def set_delivery(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    delivery: Delivery,
    actor: str,
) -> dict[str, Any]:
    """Move a memory between push and pull (specification 21.2).

    Only the user does this. A delivery an agent could set for itself would be
    the same hole the commit gate closed on preferences by another route: mark
    a memory startup_required and it is in front of every later session,
    whatever the type says.

    Either promotion is refused when the opening would no longer fit. Trimming
    it to titles instead would silently drop the standing rules the session was
    supposed to be told, and a fixed cost that quietly stops delivering is
    worse than one that says it is full.

    Both are measured, and against the opening a session actually receives.
    scope_required went unchecked entirely, and startup_required was weighed
    against the startup pack alone — so thirteen rules could be promoted one
    after another, each accepted, and leave every scoped session opening
    without its current state.
    """
    from mashu import bootstrap

    delivery = Delivery(delivery)
    entity = get_entity(cur, memory_id, lock=True)

    if delivery is not Delivery.PULL_ONLY and entity["active_version"] is not None:
        version = get_version(cur, entity["active_version"])
        # None means the heaviest opening, which is the case a promotion into
        # the startup pack has to survive: it rides with every scope in turn.
        against = entity["scope_id"] if delivery is Delivery.SCOPE_REQUIRED else None
        fits, cost = bootstrap.would_fit(
            cur,
            memory_id=memory_id,
            content=version["directive"] or version["content"],
            scope_id=against,
        )
        if not fits:
            raise DeliveryError(
                f"a session opening would come to {cost} token, over the ceiling of "
                f"{bootstrap.BOOTSTRAP_TOKEN_BUDGET}. Shorten a directive, or take "
                f"something else out of the pack first"
            )

    cur.execute(
        "UPDATE memory_entity SET delivery = %s WHERE memory_id = %s",
        (str(delivery), memory_id),
    )
    events.record(
        cur,
        EventType.DELIVERY_SET,
        actor,
        memory_id=memory_id,
        detail={"from": entity["delivery"], "to": str(delivery)},
    )
    return get_entity(cur, memory_id)


def set_directive(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    directive: str | None,
    actor: str,
) -> dict[str, Any]:
    """Give an adopted memory its short standing form (specification 21.2).

    The directive is the same knowledge as the content, compressed to the rule
    a person would actually be told at a session opening. So writing one is not
    a new claim and does not make a version: an identical body sitting twice in
    the history would read as a change of mind about the content, which is the
    reason section 8 gives for correcting a type in place rather than by
    revision.

    Only the user writes one, for the same reason only the user sets delivery.
    The directive is the text pushed into every session that receives this
    memory, so an agent able to write its own would be writing standing
    instructions by another route — the hole section 17 closed on preferences,
    reopened one column over. The person typing it at the terminal is the
    review that migration 0010 requires: a summary made at read time is an
    interpretation nobody agreed to.

    A memory that is already pushed has its pack re-measured before the change
    is taken, and an over-budget one is handed back rather than trimmed (21.2).
    Shortening is the whole point of a directive, so this refuses in practice
    only when the new one is longer than what it replaces.
    """
    from mashu import bootstrap

    entity = get_entity(cur, memory_id, lock=True)
    if EntityStatus(entity["status"]) is EntityStatus.MERGED:
        raise MergeError(f"entity {memory_id} was merged into {entity['merged_into']}")

    version_id = entity["active_version"]
    if version_id is None:
        raise NotFoundError(
            f"{entity['title']} has no active version; a directive is the short form "
            f"of something the store currently holds as true"
        )

    delivery = Delivery(entity["delivery"])
    if directive is not None and delivery is not Delivery.PULL_ONLY:
        scope_id = entity["scope_id"] if delivery is Delivery.SCOPE_REQUIRED else None
        fits, cost = bootstrap.would_fit(
            cur, memory_id=memory_id, content=directive, scope_id=scope_id
        )
        if not fits:
            raise DeliveryError(
                f"this directive puts the {delivery} pack at {cost} token, over "
                f"{bootstrap.BOOTSTRAP_TOKEN_BUDGET}; shorten it, or move something "
                f"else out of the pack first"
            )

    previous = get_version(cur, version_id)["directive"]
    cur.execute(
        "UPDATE memory_version SET directive = %s WHERE version_id = %s",
        (directive, version_id),
    )
    events.record(
        cur,
        EventType.DIRECTIVE_SET,
        actor,
        memory_id=memory_id,
        version_id=version_id,
        detail={"had_one": previous is not None, "cleared": directive is None},
    )
    return {"memory_id": memory_id, "title": entity["title"], "directive": directive}


def set_entity_status(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    target: EntityStatus,
    actor: str,
    reason: str,
) -> None:
    """Settle what an entity is, once a review has decided (specification 20.1).

    This is how a provisional entity becomes a real one. Merging is not done
    here: it moves content between entities and has its own procedure.
    """
    target = EntityStatus(target)
    if target is EntityStatus.MERGED:
        raise MergeError("use merge_entities to merge; it has to move the versions")

    entity = get_entity(cur, memory_id, lock=True)
    previous = EntityStatus(entity["status"])
    if previous is EntityStatus.MERGED:
        raise MergeError(f"entity {memory_id} was merged into {entity['merged_into']}")

    cur.execute(
        "UPDATE memory_entity SET status = %s WHERE memory_id = %s",
        (str(target), memory_id),
    )
    events.record(
        cur,
        EventType.STATUS_CHANGED,
        actor,
        memory_id=memory_id,
        detail={"entity_from": str(previous), "entity_to": str(target), "reason": reason},
    )


def set_type(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    target: MemoryType,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Correct what kind of thing an entity is (specification 8).

    Nothing about the content moves. The type is a reading of what the entity
    already says, so a correction to it is not a new version: adding one would
    put an identical body in the history and make the change look like a change
    of mind about the content.

    It is not a light operation even so. The type decides which commit line the
    entity's later changes take (17), which is why a proposal to do this waits
    for a person.

    delivery is deliberately left alone. An entity retyped to state does not
    join the session-start pack, because joining it is the operation that has
    to pass admission control (21.2).
    """
    target = MemoryType(target)
    entity = get_entity(cur, memory_id, lock=True)
    previous = MemoryType(entity["type"])

    if EntityStatus(entity["status"]) is EntityStatus.MERGED:
        raise MergeError(f"entity {memory_id} was merged into {entity['merged_into']}")
    if previous is target:
        raise MashuError(f"entity {memory_id} is already {target}")

    cur.execute("UPDATE memory_entity SET type = %s WHERE memory_id = %s", (str(target), memory_id))
    events.record(
        cur,
        EventType.TYPE_CORRECTED,
        actor,
        memory_id=memory_id,
        detail={"from": str(previous), "to": str(target), "reason": reason},
    )
    return {"memory_id": memory_id, "title": entity["title"], "from": previous, "to": target}


def merge_entities(
    cur: psycopg.Cursor,
    *,
    source: UUID,
    target: UUID,
    actor: str,
    reason: str,
    keep_active: UUID | None = None,
) -> dict[str, Any]:
    """Merge the source entity into the target (specification 20.2).

    Only the user does this. keep_active names which of the two active versions
    survives when both entities had one; the other is superseded. It is left to
    the caller because the choice is about which reading is true, and the system
    does not decide that.

    The source row is kept, marked merged and pointing at the target. Past
    context assemblies recorded its memory id, and deleting it would break those
    judgement logs.
    """
    if source == target:
        raise MergeError("an entity cannot be merged into itself")

    source_row = get_entity(cur, source, lock=True)
    target_row = get_entity(cur, target, lock=True)
    for row in (source_row, target_row):
        if EntityStatus(row["status"]) is EntityStatus.MERGED:
            raise MergeError(f"entity {row['memory_id']} was already merged")

    survivor = _choose_surviving_active(source_row, target_row, keep_active)

    # The pointers have to come off first: an entity may only point at its own
    # versions, so moving the versions while the source still points at them
    # would break the composite foreign key.
    cur.execute(
        "UPDATE memory_entity SET active_version = NULL, latest_version = NULL "
        "WHERE memory_id = %s",
        (source,),
    )
    cur.execute(
        "UPDATE memory_version SET memory_id = %s WHERE memory_id = %s",
        (target, source),
    )
    moved = cur.rowcount

    retired = [
        v
        for v in (source_row["active_version"], target_row["active_version"])
        if v is not None and v != survivor
    ]
    cur.execute(
        "UPDATE memory_entity SET active_version = NULL WHERE memory_id = %s",
        (target,),
    )
    for version_id in retired:
        set_status(
            cur,
            version_id=version_id,
            target=VersionStatus.SUPERSEDED,
            actor=actor,
            reason=f"not chosen when merging {source} into {target}: {reason}",
        )

    cur.execute(
        """
        UPDATE memory_entity SET
            active_version = %s,
            latest_version = (
                SELECT version_id FROM memory_version
                WHERE memory_id = %s
                ORDER BY created_at DESC, version_id DESC
                LIMIT 1
            )
        WHERE memory_id = %s
        """,
        (survivor, target, target),
    )

    # Deduplicate before repointing, since the pair is the primary key.
    cur.execute(
        """
        DELETE FROM memory_evidence e
        WHERE e.to_memory = %s
          AND EXISTS (
              SELECT 1 FROM memory_evidence k
              WHERE k.from_version = e.from_version AND k.to_memory = %s
          )
        """,
        (source, target),
    )
    deduplicated = cur.rowcount
    cur.execute(
        "UPDATE memory_evidence SET to_memory = %s WHERE to_memory = %s",
        (target, source),
    )
    evidence_moved = cur.rowcount

    cur.execute(
        "UPDATE memory_entity SET status = %s, merged_into = %s WHERE memory_id = %s",
        (str(EntityStatus.MERGED), target, source),
    )

    summary = {
        "source": str(source),
        "versions_moved": moved,
        "evidence_moved": evidence_moved,
        "evidence_deduplicated": deduplicated,
        "not_chosen": [str(v) for v in retired],
        "reason": reason,
    }
    events.record(cur, EventType.ENTITY_MERGED, actor, memory_id=target, detail=summary)
    return summary


def _choose_surviving_active(
    source_row: dict, target_row: dict, keep_active: UUID | None
) -> UUID | None:
    """Which version is active on the target once the merge is done."""
    candidates = [
        v for v in (target_row["active_version"], source_row["active_version"]) if v is not None
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        if keep_active is not None and keep_active not in candidates:
            raise MergeError(f"{keep_active} is not an active version of either entity")
        return candidates[0]
    if keep_active is None:
        raise MergeError(
            "both entities have an active version; name which one survives: "
            + ", ".join(str(c) for c in candidates)
        )
    if keep_active not in candidates:
        raise MergeError(f"{keep_active} is not an active version of either entity")
    return keep_active


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------
_ACTIVE_OK = frozenset({VersionStatus.CANDIDATE, VersionStatus.COMPLETED})


def scope_description(name: str, description: str | None) -> str:
    """The text scope detection matches a query against."""
    return f"{name}. {description}" if description else name


def embed_text(text: str) -> str:
    """One document vector in pgvector's text form.

    Embedding happens on write rather than in a later pass so that a memory is
    searchable the moment it exists. A candidate that could not be found until
    some batch job caught up would be invisible to layer 2 exactly while its
    review is outstanding, which is when it is most wanted.
    """
    vector = get_embedder().embed_documents([text])[0]
    return "[" + ",".join(f"{v:.8f}" for v in vector) + "]"


def _insert_version(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    content: str,
    status: VersionStatus,
    supersedes: UUID | None,
    reason: str | None,
    source_type: SourceType,
    source_reference: str | None,
    created_by: str,
    actor: str,
    directive: str | None = None,
) -> UUID:
    cur.execute(
        """
        INSERT INTO memory_version
            (memory_id, content, directive, status, supersedes, reason,
             source_type, source_reference, created_by, content_embedding)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING version_id
        """,
        (
            memory_id,
            content,
            directive,
            str(VersionStatus(status)),
            supersedes,
            reason,
            str(SourceType(source_type)),
            source_reference,
            created_by,
            embed_text(content),
        ),
    )
    version_id = cur.fetchone()["version_id"]
    detail: dict[str, Any] = {"status": str(VersionStatus(status))}
    if supersedes is not None:
        detail["supersedes"] = str(supersedes)
    events.record(
        cur,
        EventType.VERSION_CREATED,
        actor,
        memory_id=memory_id,
        version_id=version_id,
        detail=detail,
    )
    return version_id


def _set_latest(cur: psycopg.Cursor, memory_id: UUID, version_id: UUID) -> None:
    cur.execute(
        "UPDATE memory_entity SET latest_version = %s WHERE memory_id = %s",
        (version_id, memory_id),
    )


def _check_still_fits(cur: psycopg.Cursor, *, entity: dict, version_id: UUID) -> None:
    """Refuse to adopt a version that would burst the session-start pack (21.2).

    The check belongs on the pointer rather than on the delivery setting,
    because a memory that is already pushed bursts the pack by growing, not by
    being promoted. Every route to adopting a version runs through here, which
    is the only way the ceiling holds against all of them.

    Handing it back is the point. Section 21.2 refuses to trim a pushed memory
    down to its title, because what would be dropped is exactly the standing
    rule the session was going to be told, and nobody would see it go.
    """
    from mashu import bootstrap

    delivery = Delivery(entity["delivery"])
    if delivery is Delivery.PULL_ONLY:
        return

    version = get_version(cur, version_id)
    scope_id = entity["scope_id"] if delivery is Delivery.SCOPE_REQUIRED else None
    fits, cost = bootstrap.would_fit(
        cur,
        memory_id=entity["memory_id"],
        content=version["directive"] or version["content"],
        scope_id=scope_id,
    )
    if not fits:
        raise DeliveryError(
            f"adopting this version puts the {delivery} pack at {cost} token, over "
            f"{bootstrap.BOOTSTRAP_TOKEN_BUDGET}; shorten it, give it a directive, "
            f"or move something out of the pack first"
        )


def _point_active_at(
    cur: psycopg.Cursor,
    *,
    memory_id: UUID,
    version_id: UUID,
    actor: str,
    reason: str | None = None,
) -> None:
    """Move the pointer, retiring whatever it pointed at before."""
    entity = get_entity(cur, memory_id)
    previous = entity["active_version"]
    if previous == version_id:
        return

    _check_still_fits(cur, entity=entity, version_id=version_id)

    if previous is not None:
        previous_status = VersionStatus(get_version(cur, previous)["status"])
        check_transition(previous_status, VersionStatus.SUPERSEDED)
        retirement = reason or "replaced by a newly adopted version"
        cur.execute(
            "UPDATE memory_version SET status = %s, reason = %s WHERE version_id = %s",
            (str(VersionStatus.SUPERSEDED), retirement, previous),
        )
        events.record(
            cur,
            EventType.STATUS_CHANGED,
            actor,
            memory_id=memory_id,
            version_id=previous,
            detail={
                "from": str(previous_status),
                "to": str(VersionStatus.SUPERSEDED),
                "reason": retirement,
            },
        )

    cur.execute(
        "UPDATE memory_entity SET active_version = %s WHERE memory_id = %s",
        (version_id, memory_id),
    )
    events.record(
        cur,
        EventType.ACTIVE_SWITCHED,
        actor,
        memory_id=memory_id,
        version_id=version_id,
        detail={"from": str(previous) if previous else None, "reason": reason},
    )


def _clear_active(cur: psycopg.Cursor, *, memory_id: UUID, actor: str, reason: str) -> None:
    """Leave the entity with no current truth."""
    cur.execute(
        "UPDATE memory_entity SET active_version = NULL WHERE memory_id = %s",
        (memory_id,),
    )
    events.record(
        cur,
        EventType.ACTIVE_SWITCHED,
        actor,
        memory_id=memory_id,
        detail={"to": None, "reason": reason},
    )
