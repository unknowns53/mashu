from __future__ import annotations

import os
import shutil
from uuid import uuid4

import psycopg
import pytest

from mashu import (
    bootstrap,
    capacity,
    config,
    memories,
    migrate,
    nominations,
    scopes,
    topics,
)
from mashu.errors import MashuError, RefusedError
from mashu.migrate import migration_files
from mashu.tokens import pushed_cost

TRIGGER = "Before changing difficulty levers, win rates, or initial placement"
LEVER = "keep the opening win rate for the first player between 45 and 55 percent"
PLACEMENT = "never place two strong pieces adjacent in the initial layout"
ALWAYS = "report a finished run only with the output that proves it"


def topic(cur, name="difficulty", *, scope_id=None, trigger=TRIGGER):
    return topics.create_topic(cur, name=name, trigger=trigger, scope_id=scope_id, actor="user")


def filed(cur, subject, content=LEVER):
    return memories.remember(
        cur, content=content, actor="user", delivery="topic", topic_id=subject["topic_id"]
    )


def line(subject, rules):
    return capacity.topic_line(subject["name"], rules, subject["trigger"])


# bootstrap pushes the index, never the bodies


def test_bootstrap_lists_a_visible_topic_line_and_never_its_rules(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    filed(cur, subject)
    filed(cur, subject, PLACEMENT)

    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)

    assert [row["line"] for row in got["topics"]] == [
        f"difficulty (2 rules): {TRIGGER}",
    ]
    assert got["topics"][0]["rules"] == 2
    assert "memory_list(topic=<name>)" in got["topic_instruction"]
    assert LEVER not in str(got) and PLACEMENT not in str(got)
    assert got["topic_tokens"] == pushed_cost([line(subject, 2)])
    assert got["memory_tokens"] == got["topic_tokens"]


def test_empty_archived_and_other_scope_topics_are_not_listed(cur, scope_id):
    elsewhere = scopes.create_scope(cur, name="elsewhere", actor="user")["scope_id"]
    topic(cur, "empty one", scope_id=scope_id)
    archived = topic(cur, "archived one")
    topics.archive_topic(cur, archived["topic_id"], actor="user")
    away = topic(cur, "away", scope_id=elsewhere)
    filed(cur, away)

    here = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    assert here["topics"] == [] and here["topic_instruction"] is None
    unrouted = bootstrap.session_bootstrap(cur, actor="agent")
    assert unrouted["topics"] == []
    there = bootstrap.session_bootstrap(cur, actor="agent", scope_id=elsewhere)
    assert [row["topic"] for row in there["topics"]] == ["away"]


def test_a_topic_for_every_session_is_listed_everywhere(cur, scope_id):
    everywhere = topic(cur, "audio trim", trigger="Before trimming or splitting audio")
    filed(cur, everywhere, "keep a 20 ms fade at every cut so no click is heard")

    for session_scope in (None, scope_id):
        got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=session_scope)
        assert [row["line"] for row in got["topics"]] == [
            "audio trim (1 rule): Before trimming or splitting audio"
        ]


def test_the_opening_comes_in_the_documented_order(cur, scope_id):
    memories.remember(cur, content=ALWAYS, actor="user")
    subject = topic(cur, scope_id=scope_id)
    filed(cur, subject)
    got = bootstrap.session_bootstrap(cur, actor="agent", scope_id=scope_id)
    keys = list(got)
    assert (
        keys.index("always")
        < keys.index("scoped")
        < keys.index("topics")
        < keys.index("states")
        < keys.index("temporary")
        < keys.index("pending")
    )


# pulling a topic


def test_reading_a_topic_returns_its_rules_and_records_the_session(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    first = filed(cur, subject)
    second = filed(cur, subject, PLACEMENT)
    session = uuid4()

    got = topics.read_for_session(cur, "difficulty", actor="agent", session=session)

    assert got["topic"]["trigger"] == TRIGGER
    assert got["topic"]["scope"] == "test scope"
    assert {row["memory_id"] for row in got["memories"]} == {
        first["memory_id"],
        second["memory_id"],
    }
    cur.execute("SELECT actor, detail FROM event_log WHERE event_type = 'topic_read'")
    events = cur.fetchall()
    assert events == [
        {
            "actor": "agent",
            "detail": {"topic_id": str(subject["topic_id"]), "session": str(session)},
        }
    ]


def test_reading_for_a_person_records_nothing(cur):
    subject = topic(cur)
    filed(cur, subject)
    assert [row["content"] for row in topics.topic_rules(cur, "difficulty")["memories"]] == [LEVER]
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'topic_read'")
    assert cur.fetchone()["n"] == 0


def test_an_archived_topic_cannot_be_read_or_filled(cur):
    subject = topic(cur)
    topics.archive_topic(cur, subject["topic_id"], actor="user")
    with pytest.raises(MashuError, match="archived"):
        topics.topic_rules(cur, "difficulty")
    with pytest.raises(MashuError, match="archived"):
        filed(cur, subject)


def test_removing_deletes_an_unused_topic_and_archives_a_used_one(cur):
    unused = topic(cur, "unused")
    assert topics.remove_topic(cur, unused["topic_id"], actor="user")["removed"] == "deleted"
    assert topics.get_topic(cur, "unused") is None

    used = topic(cur)
    rule = filed(cur, used)
    with pytest.raises(MashuError, match="still holds 1 active rule"):
        topics.remove_topic(cur, used["topic_id"], actor="user")
    memories.retire(
        cur, rule["memory_id"], reason="lever gone", actor="user", retirement_kind="out_of_scope"
    )
    assert topics.remove_topic(cur, used["topic_id"], actor="user")["removed"] == "archived"
    assert topics.get_topic(cur, "difficulty")["archived_at"] is not None


def test_topic_names_and_triggers_are_one_bounded_line(cur):
    with pytest.raises(MashuError, match="at most 40"):
        topic(cur, "x" * 41)
    with pytest.raises(MashuError, match="one line"):
        topic(cur, trigger="first line\nsecond line")
    made = topic(cur, "  難易度  ", trigger="  難易度を調整する前に  ")
    assert made["name"] == "難易度" and made["trigger"] == "難易度を調整する前に"
    with pytest.raises(MashuError, match="already exists"):
        topic(cur, "難易度")


def test_editing_a_topic_moves_its_rules_scope_with_it(cur, scope_id):
    subject = topic(cur)
    rule = filed(cur, subject)
    assert rule["scope_id"] is None

    topics.update_topic(cur, subject["topic_id"], actor="user", scope_id=scope_id)
    assert memories.get_memory(cur, rule["memory_id"])["scope_id"] == scope_id
    listed = memories.active_memories(cur, scope_id=scope_id)
    assert [row["memory_id"] for row in listed] == [rule["memory_id"]]

    topics.update_topic(cur, subject["topic_id"], actor="user", clear_scope=True)
    assert memories.get_memory(cur, rule["memory_id"])["scope_id"] is None
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'topic_updated'")
    assert cur.fetchone()["n"] == 2


# capacity


def test_a_global_topic_line_counts_in_the_always_layer(cur, scope_id):
    subject = topic(cur)
    assert capacity.bootstrap_totals(cur)["always"] == 0  # an empty topic pushes nothing
    filed(cur, subject)
    totals = capacity.bootstrap_totals(cur)
    assert totals["always"] == pushed_cost([line(subject, 1)])
    assert totals["scopes"] == {}


def test_a_scoped_topic_line_counts_in_its_scope(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    filed(cur, subject)
    filed(cur, subject, PLACEMENT)
    totals = capacity.bootstrap_totals(cur)
    assert totals["always"] == 0
    assert totals["scopes"] == {scope_id: pushed_cost([line(subject, 2)])}
    assert capacity.topic_bodies(cur) == {subject["topic_id"]: pushed_cost([LEVER, PLACEMENT])}


def test_the_first_rule_of_a_topic_is_refused_when_its_line_does_not_fit(cur, monkeypatch):
    memories.remember(cur, content=ALWAYS, actor="user")
    subject = topic(cur)
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(pushed_cost([ALWAYS]) + 3))

    with pytest.raises(RefusedError, match="the always layer seats") as refused:
        filed(cur, subject)
    projected = pushed_cost([ALWAYS, line(subject, 1)])
    assert str(projected) in str(refused.value)
    assert str(pushed_cost([line(subject, 1)])) in str(refused.value)
    assert "this topic's index line" in str(refused.value)


def test_a_scoped_topic_line_is_refused_against_its_scope_seat(cur, scope_id, monkeypatch):
    subject = topic(cur, scope_id=scope_id)
    monkeypatch.setenv("MASHU_CAPACITY", "10")
    with pytest.raises(RefusedError, match="the opening seats 10 tokens"):
        filed(cur, subject)
    cur.execute("SELECT count(*) AS n FROM memory")
    assert cur.fetchone()["n"] == 0


def test_a_topics_bodies_have_their_own_bound(cur, monkeypatch):
    subject = topic(cur)
    filed(cur, subject)
    monkeypatch.setenv("MASHU_TOPIC_CAPACITY", str(pushed_cost([LEVER]) + 1))

    with pytest.raises(RefusedError, match="the topic 'difficulty' holds") as refused:
        filed(cur, subject, PLACEMENT)
    message = str(refused.value)
    assert str(pushed_cost([LEVER, PLACEMENT])) in message
    assert str(pushed_cost([PLACEMENT])) in message
    assert "mashu retire" in message and "another topic" in message

    # The bodies are not pushed, so the opening had room for them.
    assert capacity.bootstrap_totals(cur)["always"] < config.always_capacity()


def test_a_trigger_edit_is_weighed_where_the_line_lands(cur, monkeypatch):
    subject = topic(cur)
    filed(cur, subject)
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", str(pushed_cost([line(subject, 1)])))
    with pytest.raises(RefusedError, match="the always layer seats"):
        topics.update_topic(
            cur, subject["topic_id"], actor="user", trigger=TRIGGER + " in the enrai build"
        )
    # A shorter trigger shrinks the line, and passes.
    topics.update_topic(cur, subject["topic_id"], actor="user", trigger="Before tuning")


def test_writes_that_shrink_the_opening_pass_when_it_is_already_over(cur, scope_id, monkeypatch):
    kept = memories.remember(cur, content=ALWAYS, actor="user")
    other = memories.remember(cur, content=PLACEMENT, actor="user")
    subject = topic(cur, scope_id=scope_id)
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "10")

    # Over the layer already, and still a shorter body, a move out, and a move into a
    # scoped topic all go through: each leaves the layer lighter than it was.
    memories.revise(cur, kept["memory_id"], content="prove a finished run", actor="user")
    memories.set_delivery(cur, other["memory_id"], delivery="scope", scope_id=scope_id, actor="u")
    moved = memories.set_delivery(
        cur, kept["memory_id"], delivery="topic", topic_id=subject["topic_id"], actor="user"
    )
    assert moved["delivery"] == "topic"

    # Growing it is still refused, and so is a global topic line in place of a short body.
    with pytest.raises(RefusedError, match="the always layer seats 10"):
        memories.remember(cur, content="one more line for everyone", actor="user")
    global_topic = topic(cur, "everywhere", trigger="Before any long work")
    short = memories.remember(cur, content="x", actor="user", scope_id=scope_id, delivery="scope")
    memories.set_delivery(cur, short["memory_id"], delivery="always", actor="user")
    with pytest.raises(RefusedError, match="the always layer seats 10"):
        memories.set_delivery(
            cur,
            short["memory_id"],
            delivery="topic",
            topic_id=global_topic["topic_id"],
            actor="user",
        )


# writing into a topic


def test_remember_files_a_rule_under_its_topic_with_the_topic_scope(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    elsewhere = scopes.create_scope(cur, name="elsewhere", actor="user")["scope_id"]
    row = memories.remember(
        cur,
        content=LEVER,
        actor="user",
        delivery="topic",
        topic_id=subject["topic_id"],
        scope_id=elsewhere,
    )
    assert row["delivery"] == "topic"
    assert row["topic_id"] == subject["topic_id"]
    assert row["scope_id"] == scope_id
    detail = memories.memory_details(cur, row["memory_id"])
    assert detail["topic_name"] == "difficulty" and detail["topic_trigger"] == TRIGGER

    with pytest.raises(MashuError, match="needs the topic"):
        memories.remember(cur, content=PLACEMENT, actor="user", delivery="topic")
    with pytest.raises(MashuError, match="only applies to delivery 'topic'"):
        memories.remember(
            cur, content=PLACEMENT, actor="user", delivery="always", topic_id=subject["topic_id"]
        )


def test_delivery_can_move_into_and_out_of_a_topic(cur):
    subject = topic(cur)
    row = memories.remember(cur, content=LEVER, actor="user")
    moved = memories.set_delivery(
        cur, row["memory_id"], delivery="topic", topic_id=subject["topic_id"], actor="user"
    )
    assert (moved["delivery"], moved["topic_id"]) == ("topic", subject["topic_id"])
    back = memories.set_delivery(cur, row["memory_id"], delivery="always", actor="user")
    assert (back["delivery"], back["topic_id"]) == ("always", None)
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'delivery_changed' ORDER BY event_id"
    )
    details = [row["detail"] for row in cur.fetchall()]
    assert details[0]["to_topic_id"] == str(subject["topic_id"])
    assert details[1]["from_topic_id"] == str(subject["topic_id"])


def test_admission_can_file_a_candidate_under_a_topic(cur):
    subject = topic(cur)
    answer = nominations.remember_explicit(
        cur,
        content=LEVER,
        actor="agent",
        approval={"kind": "user_instruction", "instruction": "remember this for difficulty"},
        request_id=uuid4(),
        delivery="topic",
        topic_id=subject["topic_id"],
    )
    assert answer["admitted"] is True
    assert answer["memory"]["delivery"] == "topic"
    assert answer["memory"]["topic_id"] == str(subject["topic_id"])


def test_admission_by_id_takes_the_topic_and_its_scope(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    nominated = nominations.nominate_user_explicit(cur, content=LEVER, actor="agent")
    nomination = nominated["nomination"]
    admitted = nominations.admit(
        cur,
        nomination["nomination_id"],
        actor="user",
        delivery="topic",
        expected_version=nomination["version"],
        topic_id=subject["topic_id"],
        approval={"kind": "user_direct"},
        request_id=uuid4(),
    )
    assert admitted["topic_id"] == str(subject["topic_id"])
    assert admitted["scope_id"] == str(scope_id)


def test_a_topic_memory_cannot_become_temporary(cur):
    subject = topic(cur)
    row = filed(cur, subject)
    with pytest.raises(MashuError, match="trigger would be lost"):
        memories.convert_to_temporary(cur, row["memory_id"], days=2, actor="user")
    assert memories.get_memory(cur, row["memory_id"])["status"] == "active"


def test_a_retired_topic_rule_is_restored_into_its_topic(cur, scope_id):
    subject = topic(cur)
    row = filed(cur, subject)
    memories.retire(
        cur,
        row["memory_id"],
        reason="the lever moved",
        actor="user",
        retirement_kind="out_of_scope",
    )
    topics.update_topic(cur, subject["topic_id"], actor="user", scope_id=scope_id)
    restored = memories.restore(
        cur,
        row["memory_id"],
        reason="the lever is back",
        actor="user",
        approval_source={"kind": "user_direct"},
    )
    assert restored["scope_id"] == scope_id and restored["topic_id"] == subject["topic_id"]


# the schema


def test_migration_applies_to_a_store_at_0009_and_keeps_its_rows(tmp_path):
    admin_dsn = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
    name = f"mashu_topic_upgrade_{uuid4().hex[:10]}"
    old = tmp_path / "old-migrations"
    old.mkdir()
    for path in migration_files():
        if path.name < "0010_topics.sql":
            shutil.copy(path, old / path.name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = f"dbname={name}"
    try:
        migrate.migrate(dsn, old)
        with psycopg.connect(dsn) as conn:
            ledger_id = conn.execute(
                "INSERT INTO ledger (kind, what, prevention, created_by) "
                "VALUES ('explicit', 'seed', 'seed rule', 'test') RETURNING ledger_id"
            ).fetchone()[0]
            kept = conn.execute(
                "INSERT INTO memory (content, delivery, guard_action, evidence, created_by) "
                "VALUES ('a guard that survives', 'guard', 'Task', %s, 'test') "
                "RETURNING memory_id",
                ([ledger_id],),
            ).fetchone()[0]

        assert migrate.migrate(dsn) == ["0010_topics.sql"]
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT delivery, guard_action, topic_id FROM memory WHERE memory_id = %s",
                (kept,),
            ).fetchone()
            assert row == ("guard", "Task", None)
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO memory (content, delivery, evidence, created_by) "
                    "VALUES ('a topic rule with no topic', 'topic', %s, 'test')",
                    ([ledger_id],),
                )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# the MCP boundary


@pytest.fixture
def mcp(committing_dsn, monkeypatch):
    pytest.importorskip("mcp.server")
    import asyncio

    from mashu import server

    monkeypatch.setenv("MASHU_DATABASE_URL", committing_dsn)
    tool_server = server.build_server()

    def call(tool, **arguments):
        result = asyncio.run(tool_server.call_tool(tool, arguments))
        return result.structured_content

    return call


def test_memory_list_takes_exactly_one_of_scope_or_topic(mcp):
    assert "exactly one" in mcp("memory_list")["error"]
    assert "exactly one" in mcp("memory_list", scope="a", topic="b")["error"]


def test_memory_list_reads_a_topic_and_records_this_server_session(mcp, committing_dsn):
    from mashu import db

    name = f"topic {uuid4().hex[:8]}"
    with db.transaction(committing_dsn) as cur:
        subject = topic(cur, name, trigger="Before touching the exporter")
        filed(cur, subject, f"read the exporter manifest first {uuid4().hex}")

    first = mcp("memory_list", topic=name)
    second = mcp("memory_list", topic=name)
    assert first["ok"] is True
    assert first["topic"]["name"] == name
    assert len(first["memories"]) == 1

    with db.transaction(committing_dsn) as cur:
        cur.execute(
            "SELECT detail FROM event_log WHERE event_type = 'topic_read' "
            "AND detail ->> 'topic_id' = %s",
            (str(subject["topic_id"]),),
        )
        sessions = {row["detail"]["session"] for row in cur.fetchall()}
    assert len(sessions) == 1
    assert second["ok"] is True


def test_memory_admit_files_content_under_a_named_topic(mcp, committing_dsn):
    from mashu import db

    name = f"topic {uuid4().hex[:8]}"
    with db.transaction(committing_dsn) as cur:
        topic(cur, name, trigger="Before cutting a release branch")

    answer = mcp(
        "memory_admit",
        request_id=str(uuid4()),
        approval_kind="user_instruction",
        instruction="remember this for release branches",
        content=f"tag the release commit before branching {uuid4().hex}",
        delivery="topic",
        topic=name,
    )
    assert answer["ok"] is True
    assert answer["memory"]["delivery"] == "topic"
