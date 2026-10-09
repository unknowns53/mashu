from __future__ import annotations

from uuid import uuid4

import pytest

from conftest import remember, retire
from mashu import bootstrap, capacity, config, memories, nominations, scopes, topics
from mashu.errors import MashuError, RefusedError
from mashu.tokens import pushed_cost

TRIGGER = "Before changing difficulty levers, win rates, or initial placement"
LEVER = "keep the opening win rate for the first player between 45 and 55 percent"
PLACEMENT = "never place two strong pieces adjacent in the initial layout"
ALWAYS = "report a finished run only with the output that proves it"


def topic(cur, name="difficulty", *, scope_id=None, trigger=TRIGGER):
    return topics.create_topic(cur, name=name, trigger=trigger, scope_id=scope_id, actor="user")


def filed(cur, subject, content=LEVER):
    return remember(cur, content, delivery="topic", topic_id=subject["topic_id"])


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


# pulling a topic


def test_reading_a_topic_records_the_session_and_a_person_reading_records_nothing(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    first = filed(cur, subject)
    second = filed(cur, subject, PLACEMENT)
    session = uuid4()
    listed = topics.topic_rules(cur, "difficulty")["memories"]
    assert {row["content"] for row in listed} == {LEVER, PLACEMENT}
    cur.execute("SELECT count(*) AS n FROM event_log WHERE event_type = 'topic_read'")
    assert cur.fetchone()["n"] == 0

    got = topics.read_for_session(cur, "difficulty", actor="agent", session=session)

    assert got["topic"]["trigger"] == TRIGGER
    assert got["topic"]["scope"] == "test scope"
    assert {row["memory_id"] for row in got["memories"]} == {
        first["memory_id"],
        second["memory_id"],
    }
    cur.execute("SELECT actor, detail FROM event_log WHERE event_type = 'topic_read'")
    assert cur.fetchall() == [
        {
            "actor": "agent",
            "detail": {"topic_id": str(subject["topic_id"]), "session": str(session)},
        }
    ]


def test_removing_deletes_an_unused_topic_and_archives_a_used_one_that_stays_shut(cur):
    unused = topic(cur, "unused")
    assert topics.remove_topic(cur, unused["topic_id"], actor="user")["removed"] == "deleted"
    assert topics.get_topic(cur, "unused") is None

    used = topic(cur)
    rule = filed(cur, used)
    with pytest.raises(MashuError, match="still holds 1 active rule"):
        topics.remove_topic(cur, used["topic_id"], actor="user")
    retire(cur, rule, "lever gone", "out_of_scope")
    assert topics.remove_topic(cur, used["topic_id"], actor="user")["removed"] == "archived"
    assert topics.get_topic(cur, "difficulty")["archived_at"] is not None
    with pytest.raises(MashuError, match="archived"):
        topics.topic_rules(cur, "difficulty")
    with pytest.raises(MashuError, match="archived"):
        filed(cur, used)


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

    # A retired rule is restored into its topic and the scope the topic has by then.
    retire(cur, rule, "the lever moved", "out_of_scope")
    topics.update_topic(cur, subject["topic_id"], actor="user", scope_id=scope_id)
    restored = memories.restore(
        cur,
        rule["memory_id"],
        reason="the lever is back",
        actor="user",
        approval_source={"kind": "user_direct"},
    )
    assert restored["scope_id"] == scope_id and restored["topic_id"] == subject["topic_id"]


def test_one_action_leads_to_one_open_topic_served_where_it_is_listed(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
    rule = filed(cur, subject)
    other = topic(cur, "delegation", trigger="Before delegating")
    with pytest.raises(MashuError, match="action is letters"):
        topics.update_topic(cur, subject["topic_id"], actor="user", action="two words")
    topics.update_topic(cur, subject["topic_id"], actor="user", action="delegate")
    with pytest.raises(MashuError, match="already applies before delegate"):
        topics.update_topic(cur, other["topic_id"], actor="user", action="delegate")

    elsewhere = scopes.create_scope(cur, name="elsewhere", actor="user")["scope_id"]
    assert topics.action_rules(cur, "delegate", elsewhere) == (None, [])
    linked, rules = topics.action_rules(cur, "delegate", scope_id)
    assert linked["topic_id"] == subject["topic_id"]
    assert [row["memory_id"] for row in rules] == [rule["memory_id"]]

    topics.update_topic(cur, subject["topic_id"], actor="user", clear_action=True)
    topics.update_topic(cur, other["topic_id"], actor="user", action="delegate")
    assert topics.action_rules(cur, "delegate", scope_id)[0]["topic_id"] == other["topic_id"]


# capacity


def test_a_topic_line_counts_in_the_layer_it_lands_in(cur, scope_id):
    everywhere = topic(cur)
    assert capacity.bootstrap_totals(cur)["always"] == 0  # an empty topic pushes nothing
    filed(cur, everywhere)
    totals = capacity.bootstrap_totals(cur)
    assert totals["always"] == pushed_cost([line(everywhere, 1)])
    assert totals["scopes"] == {}

    scoped = topic(cur, "placement", scope_id=scope_id)
    filed(cur, scoped)
    filed(cur, scoped, PLACEMENT)
    totals = capacity.bootstrap_totals(cur)
    assert totals["always"] == pushed_cost([line(everywhere, 1)])
    assert totals["scopes"] == {scope_id: pushed_cost([line(scoped, 2)])}


def test_the_first_rule_of_a_topic_is_refused_when_its_line_does_not_fit(cur, monkeypatch):
    remember(cur, ALWAYS)
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
    kept = remember(cur, ALWAYS)
    other = remember(cur, PLACEMENT)
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
        remember(cur, "one more line for everyone")
    global_topic = topic(cur, "everywhere", trigger="Before any long work")
    short = remember(cur, "x", scope_id=scope_id, delivery="scope")
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
    row = remember(cur, LEVER, delivery="topic", topic_id=subject["topic_id"], scope_id=elsewhere)
    assert row["delivery"] == "topic"
    assert row["topic_id"] == subject["topic_id"]
    assert row["scope_id"] == scope_id
    detail = memories.memory_details(cur, row["memory_id"])
    assert detail["topic_name"] == "difficulty" and detail["topic_trigger"] == TRIGGER

    with pytest.raises(MashuError, match="needs the topic"):
        remember(cur, PLACEMENT, delivery="topic")
    with pytest.raises(MashuError, match="only applies to delivery 'topic'"):
        remember(cur, PLACEMENT, delivery="always", topic_id=subject["topic_id"])


def test_delivery_moves_into_and_out_of_a_topic_but_a_topic_rule_never_turns_temporary(cur):
    subject = topic(cur)
    row = remember(cur, LEVER)
    moved = memories.set_delivery(
        cur, row["memory_id"], delivery="topic", topic_id=subject["topic_id"], actor="user"
    )
    assert (moved["delivery"], moved["topic_id"]) == ("topic", subject["topic_id"])
    with pytest.raises(MashuError, match="trigger would be lost"):
        memories.convert_to_temporary(cur, row["memory_id"], days=2, actor="user")
    assert memories.get_memory(cur, row["memory_id"])["status"] == "active"
    back = memories.set_delivery(cur, row["memory_id"], delivery="always", actor="user")
    assert (back["delivery"], back["topic_id"]) == ("always", None)
    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'delivery_changed' ORDER BY event_id"
    )
    details = [row["detail"] for row in cur.fetchall()]
    assert details[0]["to_topic_id"] == str(subject["topic_id"])
    assert details[1]["from_topic_id"] == str(subject["topic_id"])


def test_admission_files_a_candidate_under_a_topic_and_its_scope(cur, scope_id):
    subject = topic(cur, scope_id=scope_id)
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
    assert answer["memory"]["scope_id"] == str(scope_id)

    nomination = nominations.nominate_user_explicit(cur, content=PLACEMENT, actor="agent")[
        "nomination"
    ]
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


# the MCP boundary


def test_memory_list_and_admit_reach_a_topic_by_name_for_this_server_session(mcp, dsn):
    from mashu import db

    assert "exactly one" in mcp("memory_list")["error"]
    assert "exactly one" in mcp("memory_list", scope="a", topic="b")["error"]
    name = f"topic {uuid4().hex[:8]}"
    with db.transaction(dsn) as cur:
        subject = topic(cur, name, trigger="Before touching the exporter")
        filed(cur, subject, f"read the exporter manifest first {uuid4().hex}")

    first = mcp("memory_list", topic=name)
    second = mcp("memory_list", topic=name)
    assert first["ok"] is True
    assert first["topic"]["name"] == name
    assert len(first["memories"]) == 1
    assert f"memory_list(topic='{name}')" in mcp("memory_list", scope=name)["error"]

    with db.transaction(dsn) as cur:
        cur.execute(
            "SELECT detail FROM event_log WHERE event_type = 'topic_read' "
            "AND detail ->> 'topic_id' = %s",
            (str(subject["topic_id"]),),
        )
        sessions = {row["detail"]["session"] for row in cur.fetchall()}
    assert len(sessions) == 1
    assert second["ok"] is True

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
