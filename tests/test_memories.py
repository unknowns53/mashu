from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from conftest import remember, retire
from mashu import cli, db, match, memories, nominations, server, temporary, topics
from mashu.errors import MalformedRequestError, MashuError, RefusedError, RetiredConflictError

RULE = "never report a run as finished without the output that proves it"
REVISED = "never report a run as finished without pasting the output that proves it"
WITHDRAWN = "the tool now refuses on its own"
PINNED = "delegation goes to the reviewer, not to another writer"
#: A rule wrapped in the story of how it was learned, past the per-entry ceiling.
STORIED = ("keep the rule alone and leave its story out " * 3).strip()


def test_a_rule_recorded_by_hand_is_active_at_once_and_its_revisions_keep_what_it_said(cur):
    memory = remember(cur, RULE)
    assert memory["status"] == "active"
    assert memory["delivery"] == "always"
    assert len(memory["evidence"]) == 1

    cur.execute("SELECT * FROM ledger WHERE ledger_id = %s", (memory["evidence"][0],))
    assert cur.fetchone()["kind"] == "explicit"

    revised = memories.revise(
        cur, memory["memory_id"], content=REVISED, actor="user", note="say what to paste"
    )
    assert revised["content"] == REVISED
    cur.execute(
        "SELECT content, note FROM memory_revision WHERE memory_id = %s ORDER BY created_at",
        (memory["memory_id"],),
    )
    rows = cur.fetchall()
    assert [row["content"] for row in rows] == [RULE, REVISED]
    assert rows[1]["note"] == "say what to paste"


def test_retiring_needs_a_clean_reason_and_a_new_kind_and_is_not_edited_back(cur):
    memory = remember(cur, RULE)
    with pytest.raises(MashuError, match="reason"):
        retire(cur, memory, "  ")
    with pytest.raises(RefusedError):
        retire(cur, memory, "superseded by SECRETMARKER9")
    assert match.similar_tombstones(cur, RULE) == []
    with pytest.raises(MashuError, match="reserved for existing data and migration"):
        memories.retire(
            cur, memory["memory_id"], reason=WITHDRAWN, actor="user", retirement_kind="legacy"
        )
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"

    retired = retire(cur, memory, WITHDRAWN)
    assert retired["status"] == "retired"
    assert retired["retire_reason"] == WITHDRAWN
    assert retired["retired_at"] is not None
    with pytest.raises(MashuError, match="retired"):
        memories.revise(cur, memory["memory_id"], content=REVISED, actor="user")
    with pytest.raises(MashuError, match="retired"):
        retire(cur, memory, "again")


def test_the_gate_says_when_it_did_not_run(cur, monkeypatch, tmp_path):
    memory = remember(cur, RULE)
    assert memory["unchecked"] is False
    assert "malformed" not in memory

    broken = tmp_path / "broken-patterns"
    broken.write_text("SECRETMARKER\\d+\n(unclosed\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(broken))
    assert retire(cur, memory, WITHDRAWN)["malformed"] == 1

    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(tmp_path / "absent"))
    # REVISED matches the retired text, so conflict handling runs first.
    tombstone = match.similar_tombstones(cur, REVISED)[0]
    rewritten = remember(cur, REVISED, acknowledged_conflicts=[tombstone["memory_id"]])
    assert rewritten["unchecked"] is True


def test_a_retired_rule_answers_with_why_it_was_withdrawn_and_stops_being_written_back(cur):
    kept = remember(cur, RULE)
    retire(cur, kept, WITHDRAWN)

    found = match.similar_tombstones(cur, RULE)
    assert [row["memory_id"] for row in found] == [kept["memory_id"]]
    assert found[0]["retire_reason"] == WITHDRAWN

    with pytest.raises(RetiredConflictError) as raised:
        remember(cur, REVISED)
    assert [row["memory_id"] for row in raised.value.tombstones] == [kept["memory_id"]]
    assert raised.value.tombstones[0]["retire_reason"] == WITHDRAWN
    # The withdrawn body is not handed back with its own tombstone.
    assert "content" not in raised.value.tombstones[0]

    cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
    assert cur.fetchone()["n"] == 0

    written = remember(cur, REVISED, acknowledged_conflicts=[kept["memory_id"]])
    assert written["status"] == "active"
    assert [row["memory_id"] for row in written["overrides"]] == [kept["memory_id"]]

    cur.execute(
        "SELECT detail FROM event_log WHERE event_type = 'memory_created' AND memory_id = %s",
        (written["memory_id"],),
    )
    assert cur.fetchone()["detail"]["overrides"] == [str(kept["memory_id"])]

    unrelated = remember(cur, "quotas on the shared queue reset at midnight every day")
    assert unrelated["overrides"] == []


def test_superseded_links_must_exist_and_cannot_form_cycles(cur):
    first = remember(cur, "the loader checks the schema fingerprint")
    second = remember(cur, "the decoder checks the payload checksum")
    retire(
        cur, first, "the decoder rule replaces it", "superseded", superseded_by=second["memory_id"]
    )

    with pytest.raises(MashuError, match="cycle"):
        retire(
            cur,
            second,
            "the loader rule replaces it",
            "superseded",
            superseded_by=first["memory_id"],
        )
    assert memories.get_memory(cur, second["memory_id"])["status"] == "active"

    with pytest.raises(MashuError, match="no successor memory"):
        retire(cur, second, "the next rule replaces it", "superseded", superseded_by=uuid4())


def test_a_move_names_what_its_delivery_needs_and_the_act_gate_is_gone(cur, scope_id):
    memory = remember(cur, RULE)
    with pytest.raises(MashuError, match="unknown delivery 'guard'"):
        memories.set_delivery(cur, memory["memory_id"], delivery="guard", actor="user")
    with pytest.raises(MashuError, match="scope"):
        memories.set_delivery(cur, memory["memory_id"], delivery="scope", actor="user")
    with pytest.raises(MashuError, match="clear"):
        memories.set_delivery(
            cur,
            memory["memory_id"],
            delivery="always",
            actor="user",
            scope_id=scope_id,
            clear_scope=True,
        )

    moved = memories.set_delivery(
        cur, memory["memory_id"], delivery="scope", actor="user", scope_id=scope_id
    )
    assert (moved["delivery"], moved["scope_id"]) == ("scope", scope_id)
    freed = memories.set_delivery(
        cur, memory["memory_id"], delivery="always", actor="user", clear_scope=True
    )
    assert freed["scope_id"] is None


def test_listing_a_scope_shows_what_lives_there_by_whatever_route(cur, scope_id):
    pushed = remember(cur, RULE, scope_id=scope_id, delivery="scope")
    subject = topics.create_topic(
        cur, name="delegation", trigger="Before delegating", scope_id=scope_id, actor="user"
    )
    pinned = remember(cur, PINNED, delivery="topic", topic_id=subject["topic_id"])
    remember(cur, "answer in the language that was asked")

    listed = {row["memory_id"] for row in memories.active_memories(cur, scope_id=scope_id)}
    assert listed == {pushed["memory_id"], pinned["memory_id"]}
    assert [row["memory_id"] for row in memories.scope_push_memories(cur, scope_id)] == [
        pushed["memory_id"]
    ]


@pytest.mark.parametrize("scoped", [False, True])
def test_a_memory_can_become_temporary_and_back_where_it_was_delivered(cur, scope_id, scoped):
    placed = {"scope_id": scope_id, "delivery": "scope"} if scoped else {}
    here = placed.get("scope_id")
    original = remember(cur, RULE, **placed)

    converted = memories.convert_to_temporary(cur, original["memory_id"], days=2, actor="user")
    context = converted["temporary"]
    assert converted["memory"]["status"] == "retired"
    assert "converted to temporary context until" in converted["memory"]["retire_reason"]
    assert context["scope_id"] == here
    assert [row["context_id"] for row in temporary.active_temporary(cur, scope_id=here)] == [
        context["context_id"]
    ]

    restored = temporary.convert_to_memory(cur, context["context_id"], actor="user")
    assert restored["memory"]["content"] == RULE
    assert restored["memory"]["delivery"] == placed.get("delivery", "always")
    assert restored["memory"]["scope_id"] == here
    assert restored["memory"]["status"] == "active"
    assert temporary.active_temporary(cur, scope_id=here) == []

    cur.execute(
        "SELECT event_type FROM event_log WHERE event_type LIKE '%%converted%%' ORDER BY event_id"
    )
    assert [row["event_type"] for row in cur.fetchall()] == [
        "memory_converted_to_temporary",
        "temporary_converted_to_memory",
    ]


def test_temporary_round_trip_does_not_override_an_unrelated_invalidated_memory(cur):
    source = remember(cur, RULE)
    retire(cur, remember(cur, REVISED), "the required output now comes from the build service")
    converted = memories.convert_to_temporary(cur, source["memory_id"], days=2, actor="user")
    context = converted["temporary"]

    with pytest.raises(RetiredConflictError):
        temporary.convert_to_memory(cur, context["context_id"], actor="user")
    assert memories.get_memory(cur, source["memory_id"])["retirement_kind"] == "relocated"
    assert [row["context_id"] for row in temporary.active_temporary(cur)] == [context["context_id"]]


def test_a_refused_conversion_keeps_its_source_active(cur, monkeypatch):
    memory = remember(cur, RULE)
    monkeypatch.setenv("MASHU_TEMPORARY_CAPACITY", "1")

    with pytest.raises(RefusedError, match="temporary share"):
        memories.convert_to_temporary(cur, memory["memory_id"], days=1, actor="user")
    assert memories.get_memory(cur, memory["memory_id"])["status"] == "active"
    assert temporary.active_temporary(cur) == []

    monkeypatch.delenv("MASHU_TEMPORARY_CAPACITY")
    context = temporary.put_temporary(cur, content="brief outage", actor="user", days=1)
    monkeypatch.setenv("MASHU_CAPACITY", "1")
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "1")

    with pytest.raises(RefusedError, match="seats 1 tokens"):
        temporary.convert_to_memory(cur, context["context_id"], actor="user")
    assert [row["context_id"] for row in temporary.active_temporary(cur)] == [context["context_id"]]


def test_a_new_body_over_its_ceiling_is_refused_on_every_write_path(dsn, mcp, capsys):
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
        nomination = nominations.nominate_user_explicit(cur, content=PINNED, actor="agent")[
            "nomination"
        ]

    def direct(write):
        def refused():
            with pytest.raises(MalformedRequestError) as caught, db.transaction(dsn) as cur:
                write(cur)
            return str(caught.value), caught.value.over_limit

        return refused

    def over_mcp(tool, **arguments):
        def refused():
            answer = mcp(tool, **arguments)
            assert answer["ok"] is False
            return answer["error"], answer["over_limit"]

        return refused

    def over_cli(*argv):
        def refused():
            assert cli.main(["--dsn", dsn, *argv]) == 1
            return capsys.readouterr().err, None

        return refused

    def instructed(**arguments):
        return over_mcp(
            "memory_admit",
            request_id=str(uuid4()),
            approval_kind="user_instruction",
            instruction="Remember this rule",
            **arguments,
        )

    body, why = memories.CONTENT_LIMIT, memories.WHY_LIMIT
    cases = [
        ("memory_admit", instructed(content=STORIED), "content", body, "in why"),
        ("memory_admit why", instructed(content=REVISED, why="w" * 501), "why", why, "reference"),
        (
            "memory_nominate",
            over_mcp("memory_nominate", content=STORIED),
            "content",
            body,
            "in why",
        ),
        ("mashu remember", over_cli("remember", STORIED), "content", body, "in --why"),
        (
            "remember, as the TUI and a temporary conversion call it",
            direct(lambda cur: memories.remember(cur, content=STORIED, actor="user")),
            "content",
            body,
            "in why",
        ),
        (
            "memory revise",
            direct(
                lambda cur: memories.revise(cur, memory["memory_id"], content=STORIED, actor="user")
            ),
            "content",
            body,
            "out of it",
        ),
        (
            "candidate revise",
            direct(
                lambda cur: nominations.revise(
                    cur, nomination["nomination_id"], content=STORIED, actor="user"
                )
            ),
            "content",
            body,
            "out of it",
        ),
        (
            "admission with new wording",
            direct(
                lambda cur: nominations.admit(
                    cur,
                    nomination["nomination_id"],
                    actor="user",
                    delivery="always",
                    expected_version=nomination["version"],
                    content=STORIED,
                    approval={"kind": "user_direct", "conflict_ids": []},
                    request_id=uuid4(),
                )
            ),
            "content",
            body,
            "out of it",
        ),
    ]
    for path, refused, field, limit, where in cases:
        text, over = refused()
        actual = 501 if field == "why" else len(STORIED)
        assert f"{field} is {actual} chars and the limit is {limit}" in text, path
        assert where in text, path
        if over is not None:
            assert over == [{"field": field, "limit": limit, "actual": actual, "unit": "chars"}]

    with db.transaction(dsn) as cur:
        cur.execute("SELECT content FROM memory UNION ALL SELECT content FROM nomination")
        assert sorted(row["content"] for row in cur.fetchall()) == sorted([RULE, PINNED])


def test_a_long_body_carried_over_unchanged_still_passes(cur, scope_id, monkeypatch):
    def written_before_the_ceiling(write):
        with monkeypatch.context() as before:
            before.setattr(memories, "CONTENT_LIMIT", 10_000)
            return write()

    def admitted_unread(body):
        nomination = written_before_the_ceiling(
            lambda: nominations.nominate_user_explicit(cur, content=body, actor="agent")
        )["nomination"]
        return nominations.admit(
            cur,
            nomination["nomination_id"],
            actor="user",
            delivery="always",
            expected_version=nomination["version"],
            approval={"kind": "user_direct", "conflict_ids": []},
            request_id=uuid4(),
        )

    def revised_unchanged(body):
        memory = written_before_the_ceiling(lambda: remember(cur, body))
        return memories.revise(cur, memory["memory_id"], content=body, actor="user")

    def restored(body):
        memory = written_before_the_ceiling(lambda: remember(cur, body))
        retire(cur, memory, "the project paused", kind="out_of_scope")
        return memories.restore(
            cur,
            memory["memory_id"],
            reason="the project resumed",
            actor="user",
            approval_source={"kind": "user_direct"},
        )

    def redelivered(body):
        memory = written_before_the_ceiling(lambda: remember(cur, body))
        return memories.set_delivery(
            cur, memory["memory_id"], delivery="scope", scope_id=scope_id, actor="user"
        )

    cases = [
        ("amber", admitted_unread),
        ("cobalt", revised_unchanged),
        ("violet", restored),
        ("walnut", redelivered),
    ]
    for word, carry in cases:
        body = f"{word} " * 30
        assert len(body) > memories.CONTENT_LIMIT
        row = carry(body)
        assert (row["content"], row["status"]) == (body, "active"), carry.__name__


def test_why_is_kept_on_the_evidence_that_memory_get_returns(mcp):
    why = "a body averaging 200 chars hid the rule under the measurement that taught it"
    answer = mcp(
        "memory_admit",
        request_id=str(uuid4()),
        approval_kind="user_instruction",
        instruction="Remember this rule",
        content=RULE,
        why=why,
    )
    assert answer["ok"] is True

    got = mcp("memory_get", memory_id=answer["memory"]["memory_id"])["memory"]
    assert got["content"] == RULE
    assert [row["what"] for row in got["basis"]] == [why]


def test_a_banned_pattern_in_why_is_refused_under_its_own_name(mcp):
    answer = mcp("memory_nominate", content=RULE, why="first seen at SECRETMARKER7")
    assert answer["ok"] is False
    assert answer["field"] == "why"


def test_the_tools_that_take_a_body_publish_its_ceiling(monkeypatch):
    pytest.importorskip("mcp.server")
    monkeypatch.setenv("MASHU_DATABASE_URL", "dbname=mashu_test_never_created")
    tools = {tool.name: tool for tool in asyncio.run(server.build_server().list_tools())}
    for name, rest in (
        ("memory_admit", "`why`"),
        ("memory_nominate", "`why`"),
        ("pain_report", "`what`"),
    ):
        assert f"{memories.CONTENT_LIMIT} chars" in tools[name].description, name
        assert rest in tools[name].description, name
