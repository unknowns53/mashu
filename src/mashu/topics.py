"""Group rules needed only while one kind of work is being done (docs/mashu-v2.md 6)."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import psycopg

from mashu import approvals, capacity, events, redact
from mashu.errors import MashuError, RefusedError
from mashu.tokens import pushed_cost

NAME_LIMIT = 40
TRIGGER_LIMIT = 160
ACTION_LIMIT = 40
_ACTION = re.compile(r"[A-Za-z0-9_.-]+")

#: What bootstrap tells the Agent to do with the index it pushes.
INSTRUCTION = (
    "Topics list rules that apply only during one kind of work. When the work at hand "
    "matches a topic's trigger, call memory_list(topic=<name>) and read its rules before "
    "starting that work."
)

_LISTED = """
SELECT t.*, s.name AS scope_name,
       count(m.memory_id) AS rules,
       coalesce(array_agg(m.content ORDER BY m.created_at, m.memory_id)
                FILTER (WHERE m.memory_id IS NOT NULL), '{}') AS contents
FROM topic t
LEFT JOIN scope s ON s.scope_id = t.scope_id
LEFT JOIN memory m ON m.topic_id = t.topic_id AND m.status = 'active' AND m.delivery = 'topic'
WHERE (%(archived)s OR t.archived_at IS NULL)
GROUP BY t.topic_id, s.name
ORDER BY t.name
"""


def _text(value: str | None, label: str, limit: int) -> str:
    """One trimmed line, or a refusal saying what a topic's field has to be."""
    text = (value or "").strip()
    if not text:
        raise MashuError(f"a topic needs a {label}")
    if "\n" in text or "\r" in text:
        raise MashuError(f"a topic's {label} is one line")
    if len(text) > limit:
        raise MashuError(f"a topic's {label} is at most {limit} characters (this is {len(text)})")
    return text


def _clean(name: str | None, trigger: str | None) -> tuple[str, str]:
    name = _text(name, "name", NAME_LIMIT)
    trigger = _text(trigger, "trigger", TRIGGER_LIMIT)
    redact.gate({"topic": name, "topic_trigger": trigger})
    return name, trigger


def validate_new(name: str | None, trigger: str | None) -> tuple[str, str]:
    """The cleaned name and trigger a topic would be created with, or the refusal."""
    return _clean(name, trigger)


def clean_action(value: str | None) -> str:
    """The action a topic stands before: the word a PreToolUse hook maps a tool call to."""
    if not (value or "").strip():
        raise MashuError("a topic's action cannot be empty; clear it instead")
    action = _text(value, "action", ACTION_LIMIT)
    if not _ACTION.fullmatch(action):
        raise MashuError(
            f"a topic's action is letters, digits, '_', '.', or '-' (got '{action}'), "
            "such as delegate"
        )
    return action


def has_actions(cur: psycopg.Cursor) -> bool:
    """Whether this store's topics can carry an action yet."""
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass('topic') AND attname = 'action' AND NOT attisdropped
        ) AS present
        """
    )
    return cur.fetchone()["present"]


def _names(cur: psycopg.Cursor) -> str:
    cur.execute("SELECT name FROM topic WHERE archived_at IS NULL ORDER BY name")
    known = [row["name"] for row in cur.fetchall()]
    return ", ".join(known) if known else "none yet"


def get_topic(cur: psycopg.Cursor, name: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT t.*, s.name AS scope_name
        FROM topic t LEFT JOIN scope s ON s.scope_id = t.scope_id
        WHERE t.name = %s
        """,
        ((name or "").strip(),),
    )
    return cur.fetchone()


def require_topic(cur: psycopg.Cursor, name: str) -> dict[str, Any]:
    """The open topic by that name, or an error naming the ones that exist."""
    row = get_topic(cur, name)
    if row is None:
        raise MashuError(f"no topic named '{name}' (existing topics: {_names(cur)})")
    if row["archived_at"] is not None:
        raise MashuError(f"topic '{name}' is archived and receives no rules")
    return row


def unknown(cur: psycopg.Cursor, name: str) -> MashuError:
    """The error for a topic an agent named without what opening it takes."""
    return MashuError(
        f"no topic named '{name}' (existing topics: {_names(cur)}). To open it with this "
        "rule, also pass topic_trigger, one sentence saying when to read it, and the scope "
        "it is listed in, or none for every session"
    )


def for_filing(
    cur: psycopg.Cursor, name: str, trigger: str | None, scope_id: UUID | None
) -> tuple[UUID | None, dict[str, Any] | None]:
    """The open topic an instructed rule is filed under, or the topic it opens.

    Returns the existing topic's id, or the cleaned name, trigger, and scope of a topic to
    open once the rule is sure to land. A trigger for an existing topic is accepted only
    when it and the scope are the ones the topic has, so a resent request reads the same.
    """
    existing = get_topic(cur, name)
    if existing is None:
        if trigger is None:
            raise unknown(cur, name)
        name, trigger = validate_new(name, trigger)
        return None, {"name": name, "trigger": trigger, "scope_id": scope_id}
    if trigger is not None and (trigger.strip(), scope_id) != (
        existing["trigger"],
        existing["scope_id"],
    ):
        raise MashuError(
            f"topic '{existing['name']}' already exists, read in "
            f"{existing['scope_name'] or 'every session'} when: {existing['trigger']}; "
            "omit topic_trigger to file under it"
        )
    return require_topic(cur, name)["topic_id"], None


def open_instructed(
    cur: psycopg.Cursor, topic: dict[str, Any], *, approval: dict[str, Any], actor: str
) -> UUID:
    """Open the topic an instructed write files its rule under, keeping whose words opened it."""
    source = approvals.validate(
        {key: approval.get(key) for key in ("kind", "instruction", "conversation_ref")}
    )
    return create_topic(cur, actor=actor, approval_source=source, **topic)["topic_id"]


def lock_open_topic(cur: psycopg.Cursor, topic_id: UUID) -> dict[str, Any]:
    """The topic a write is about to file a rule under, held against a concurrent archive."""
    cur.execute("SELECT * FROM topic WHERE topic_id = %s FOR SHARE", (topic_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no topic {topic_id}")
    if row["archived_at"] is not None:
        raise MashuError(f"topic '{row['name']}' is archived and receives no rules")
    return row


def create_topic(
    cur: psycopg.Cursor,
    *,
    name: str,
    trigger: str,
    actor: str,
    scope_id: UUID | None = None,
    approval_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open a topic. An empty one pushes nothing, so no seat is checked.

    An agent opens one only on a user's instruction, whose `approval_source` the event keeps.
    """
    name, trigger = _clean(name, trigger)
    existing = get_topic(cur, name)
    if existing is not None:
        state = " (archived)" if existing["archived_at"] is not None else ""
        raise MashuError(f"topic '{name}' already exists{state}")
    cur.execute(
        """
        INSERT INTO topic (name, scope_id, trigger, created_by)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (name, scope_id, trigger, actor),
    )
    row = cur.fetchone()
    detail = {
        "topic_id": str(row["topic_id"]),
        "name": name,
        "trigger": trigger,
        "scope_id": str(scope_id) if scope_id else None,
    }
    if approval_source is not None:
        detail["approval_source"] = approval_source
    events.record(cur, "topic_created", actor, detail=detail)
    return row


def update_topic(
    cur: psycopg.Cursor,
    topic_id: UUID,
    *,
    actor: str,
    name: str | None = None,
    trigger: str | None = None,
    scope_id: UUID | None = None,
    clear_scope: bool = False,
    action: str | None = None,
    clear_action: bool = False,
) -> dict[str, Any]:
    """Rename, reword, move, or link a topic, weighing its index line where it will land."""
    if clear_scope and scope_id is not None:
        raise MashuError("pass a scope or clear it, not both")
    if clear_action and action is not None:
        raise MashuError("pass an action or clear it, not both")
    cur.execute("SELECT * FROM topic WHERE topic_id = %s FOR UPDATE", (topic_id,))
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no topic {topic_id}")
    if current["archived_at"] is not None:
        raise MashuError(f"topic '{current['name']}' is archived")
    new_name, new_trigger = _clean(
        current["name"] if name is None else name,
        current["trigger"] if trigger is None else trigger,
    )
    home = None if clear_scope else (current["scope_id"] if scope_id is None else scope_id)
    new_action = None if clear_action else (current["action"] if action is None else action)
    if new_action is not None and new_action != current["action"]:
        new_action = clean_action(new_action)
        cur.execute(
            "SELECT name FROM topic WHERE action = %s AND archived_at IS NULL AND topic_id <> %s",
            (new_action, topic_id),
        )
        holder = cur.fetchone()
        if holder is not None:
            raise MashuError(
                f"topic '{holder['name']}' already applies before {new_action}; one action "
                "leads to one topic, so clear that topic's action first"
            )
    if new_name != current["name"]:
        taken = get_topic(cur, new_name)
        if taken is not None:
            raise MashuError(f"topic '{new_name}' already exists")
    before = (current["name"], current["trigger"], current["scope_id"], current["action"])
    if (new_name, new_trigger, home, new_action) == before:
        return current

    verdict = capacity.check_topic_edit(
        cur, topic_id, name=new_name, trigger=new_trigger, scope_id=home
    )
    if not verdict["ok"]:
        raise RefusedError(verdict["refusal"])
    cur.execute(
        """
        UPDATE topic SET name = %s, trigger = %s, scope_id = %s, action = %s, updated_at = now()
        WHERE topic_id = %s
        RETURNING *
        """,
        (new_name, new_trigger, home, new_action, topic_id),
    )
    row = cur.fetchone()
    if home != current["scope_id"]:
        # A topic Memory carries its topic's scope so matching by scope keeps finding it.
        cur.execute(
            """
            UPDATE memory SET scope_id = %s, updated_at = now()
            WHERE topic_id = %s AND status = 'active' AND delivery = 'topic'
            """,
            (home, topic_id),
        )
    events.record(
        cur,
        "topic_updated",
        actor,
        detail={
            "topic_id": str(topic_id),
            "from": {
                "name": current["name"],
                "trigger": current["trigger"],
                "scope_id": str(current["scope_id"]) if current["scope_id"] else None,
                "action": current["action"],
            },
            "to": {
                "name": new_name,
                "trigger": new_trigger,
                "scope_id": str(home) if home else None,
                "action": new_action,
            },
        },
    )
    return row


def archive_topic(cur: psycopg.Cursor, topic_id: UUID, *, actor: str) -> dict[str, Any]:
    """Close an empty topic; its name stays taken so old events still read the same."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s, %s)",
        (capacity.LOCK_NAMESPACE, capacity.LOCK_ADMISSION),
    )
    cur.execute("SELECT * FROM topic WHERE topic_id = %s FOR UPDATE", (topic_id,))
    current = cur.fetchone()
    if current is None:
        raise MashuError(f"no topic {topic_id}")
    if current["archived_at"] is not None:
        raise MashuError(f"topic '{current['name']}' is already archived")
    cur.execute(
        "SELECT count(*) AS n FROM memory "
        "WHERE topic_id = %s AND status = 'active' AND delivery = 'topic'",
        (topic_id,),
    )
    held = cur.fetchone()["n"]
    if held:
        raise MashuError(
            f"topic '{current['name']}' still holds {held} active rule(s); retire them or "
            "move them to another delivery before archiving it"
        )
    cur.execute(
        "UPDATE topic SET archived_at = now(), updated_at = now() WHERE topic_id = %s RETURNING *",
        (topic_id,),
    )
    row = cur.fetchone()
    events.record(
        cur,
        "topic_archived",
        actor,
        detail={"topic_id": str(topic_id), "name": current["name"]},
    )
    return row


def remove_topic(cur: psycopg.Cursor, topic_id: UUID, *, actor: str) -> dict[str, Any]:
    """Delete a topic nothing has used, or archive one whose past rules or proposals name it.

    Retired rules and proposals keep their topic_id, so a used topic stays as an archived
    row and its name stays taken; an unused one leaves no reference and frees its name.
    """
    cur.execute(
        """
        SELECT EXISTS (SELECT 1 FROM memory WHERE topic_id = %(id)s)
            OR EXISTS (SELECT 1 FROM memory_change WHERE successor_topic_id = %(id)s)
            OR EXISTS (
                SELECT 1 FROM memory_change m JOIN topic t ON t.name = m.successor_topic_name
                WHERE t.topic_id = %(id)s AND m.status = 'pending'
            ) AS used
        """,
        {"id": topic_id},
    )
    if cur.fetchone()["used"]:
        return {**archive_topic(cur, topic_id, actor=actor), "removed": "archived"}
    cur.execute("DELETE FROM topic WHERE topic_id = %s RETURNING *", (topic_id,))
    row = cur.fetchone()
    if row is None:
        raise MashuError(f"no topic {topic_id}")
    events.record(
        cur,
        "topic_deleted",
        actor,
        detail={"topic_id": str(topic_id), "name": row["name"]},
    )
    return {**row, "removed": "deleted"}


def list_topics(cur: psycopg.Cursor, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Every open topic with its rule count, body cost, and index-line cost."""
    cur.execute(_LISTED, {"archived": include_archived})
    rows = cur.fetchall()
    for row in rows:
        contents = list(row.pop("contents") or [])
        row["body_tokens"] = pushed_cost(contents)
        row["line"] = capacity.topic_line(row["name"], row["rules"], row["trigger"])
        row["line_tokens"] = pushed_cost([row["line"]]) if row["rules"] else 0
    return rows


def session_index(cur: psycopg.Cursor, scope_id: UUID | None) -> list[dict[str, Any]]:
    """The topics a session in this scope is told exist: open, non-empty, and visible here."""
    # The SessionStart hook prints nothing when bootstrap fails, so a store whose topic
    # migration is still pending must get the rest of its opening rather than an error.
    cur.execute("SELECT to_regclass('topic') IS NOT NULL AS present")
    if not cur.fetchone()["present"]:
        return []
    return [
        {
            "topic": row["name"],
            "rules": row["rules"],
            "trigger": row["trigger"],
            "line": row["line"],
        }
        for row in list_topics(cur)
        if row["rules"] and row["scope_id"] in (None, scope_id)
    ]


def topic_rules(cur: psycopg.Cursor, name: str) -> dict[str, Any]:
    """One open topic and the bodies of its active rules, for the work its trigger names."""
    topic = require_topic(cur, name)
    cur.execute(
        """
        SELECT memory_id, content, created_at, updated_at FROM memory
        WHERE topic_id = %s AND status = 'active' AND delivery = 'topic'
        ORDER BY created_at, memory_id
        """,
        (topic["topic_id"],),
    )
    return {
        "topic": {
            "topic_id": topic["topic_id"],
            "name": topic["name"],
            "trigger": topic["trigger"],
            "scope": topic["scope_name"],
            "action": topic.get("action"),
        },
        "memories": cur.fetchall(),
    }


def action_rules(
    cur: psycopg.Cursor, action: str, scope_id: UUID | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The open topic a tool call's action leads to, if visible here, and its active rules."""
    # The PreToolUse hook reads this before every mapped tool call, so a store whose
    # action migration is still pending answers with nothing rather than an error.
    if not has_actions(cur):
        return None, []
    cur.execute(
        """
        SELECT * FROM topic
        WHERE action = %(action)s AND archived_at IS NULL
          AND (scope_id IS NULL OR scope_id = %(scope)s::uuid)
        """,
        {"action": action, "scope": scope_id},
    )
    topic = cur.fetchone()
    if topic is None:
        return None, []
    cur.execute(
        """
        SELECT memory_id, content FROM memory
        WHERE topic_id = %s AND status = 'active' AND delivery = 'topic'
        ORDER BY created_at, memory_id
        """,
        (topic["topic_id"],),
    )
    return topic, cur.fetchall()


def read_for_session(
    cur: psycopg.Cursor, name: str, *, actor: str, session: UUID
) -> dict[str, Any]:
    """The same read, recorded against the session so a later pain can tell it was read."""
    answer = topic_rules(cur, name)
    events.record(
        cur,
        "topic_read",
        actor,
        detail={"topic_id": str(answer["topic"]["topic_id"]), "session": str(session)},
    )
    return answer
