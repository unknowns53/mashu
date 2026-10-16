from __future__ import annotations

import datetime as dt

import pytest

from conftest import assert_steady_layout, keys, retire
from mashu import application, db, memories, memory_ui, scopes, screen, temporary, topics

RULE = "never report a run as finished without the output that proves it"
REVISED = "never report a run as finished without pasting the output that proves it"

pytestmark = pytest.mark.usefixtures("known_screen")


@pytest.fixture
def dsn(dsn):
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="deployments", actor="user")
    return dsn


def remember(dsn: str, content: str = RULE, **kwargs):
    with db.transaction(dsn) as cur:
        return memories.remember(cur, content=content, actor="user", **kwargs)


def memory_row(dsn: str, memory_id):
    with db.transaction(dsn) as cur:
        return memories.get_memory(cur, memory_id)


def test_views_show_active_retired_and_unexpired_temporary_rows(dsn, monkeypatch, capsys):
    active = remember(dsn)
    retired = remember(dsn, "keep the old deployment reason visible")
    with db.transaction(dsn) as cur:
        retire(cur, retired, "the deployer enforces this now")
        scope = scopes.require_scope(cur, "deployments")
        context = temporary.put_temporary(
            cur, content="the canary is paused", actor="user", days=2, scope_id=scope["scope_id"]
        )

    keys(monkeypatch, "2", "3", "q")
    assert memory_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert str(active["memory_id"])[:8] in out
    assert str(retired["memory_id"])[:8] in out
    assert "the deployer enforces this now" in out
    assert str(context["context_id"])[:8] in out
    assert "deployments" in out
    assert "the canary is paused" in out


def test_long_detail_pages_through_evidence_and_revision_history(dsn, monkeypatch, capsys):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        for n, content in enumerate([REVISED, *(f"wording {i}" for i in range(8))]):
            memories.revise(
                cur, memory["memory_id"], content=content, actor="user", note=f"note {n}"
            )
    monkeypatch.setenv("LINES", "12")
    keys(monkeypatch, "enter", *("space" for _ in range(12)), "b", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "more line(s), space to go on" in out
    assert "note 8" in out
    assert "evidence" in out
    assert "explicit" in out
    assert f"prevention: {RULE}" in out
    assert "revisions" in out
    assert RULE in out and REVISED in out


def test_search_is_case_insensitive_zero_results_can_be_researched_and_left_clears(
    dsn, monkeypatch, capsys
):
    remember(dsn)
    remember(dsn, "spell out the TIMEZONE in scheduled jobs")
    keys(monkeypatch, "/", "absent", "/", "timezone", "left", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "0/2" in out and "no rows match" in out
    assert "1/2" in out and "timezone" in out
    assert out.count("active memories  2") >= 2


def test_new_durable_memory_can_choose_a_scope_delivery_and_keep_its_reason(dsn, monkeypatch):
    why = "a deploy once started against an unmigrated schema"
    keys(monkeypatch, "n", RULE, why, "scope", "deployments", "q")

    assert memory_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM memory")
        row = cur.fetchone()
        scope = scopes.require_scope(cur, "deployments")
        basis = memories.memory_details(cur, row["memory_id"])["basis"]
    assert row["content"] == RULE
    assert row["delivery"] == "scope"
    assert row["scope_id"] == scope["scope_id"]
    assert row["created_by"] == "user"
    assert [entry["what"] for entry in basis] == [why]


def test_retirement_selects_a_kind_and_applies_after_entering_its_reason(dsn, monkeypatch):
    memory = remember(dsn)
    keys(monkeypatch, "r", "invalidated", "reason", "q")

    assert memory_ui.run(dsn) == 0
    row = memory_row(dsn, memory["memory_id"])
    assert row["status"] == "retired"
    assert row["retirement_kind"] == "invalidated"
    assert row["retire_reason"] == "reason"


def test_retirement_cancellation_and_legacy_selection_leave_the_memory_active(dsn, monkeypatch):
    memory = remember(dsn)
    monkeypatch.setattr(screen, "editline", lambda *_args: screen.Cancelled())
    memory_ui._retire(dsn, memory)
    assert memory_row(dsn, memory["memory_id"])["status"] == "active"

    monkeypatch.setattr(screen, "editline", lambda *_args: screen.Submitted("legacy"))
    assert "choose a supported" in memory_ui._retire(dsn, memory)
    assert memory_row(dsn, memory["memory_id"])["status"] == "active"


def test_delivery_edit_keeps_the_selection_and_its_defaults_on_empty_input(dsn, monkeypatch):
    selected = remember(dsn)
    other = remember(dsn, "spell out the timezone in scheduled jobs")
    keys(monkeypatch, "d", "scope", "deployments", "e", REVISED, "d", "", "", "q")

    assert memory_ui.run(dsn) == 0
    row = memory_row(dsn, selected["memory_id"])
    assert (row["content"], row["delivery"]) == (REVISED, "scope")
    assert memory_row(dsn, other["memory_id"])["content"] == other["content"]
    with db.transaction(dsn) as cur:
        assert row["scope_id"] == scopes.require_scope(cur, "deployments")["scope_id"]
        cur.execute(
            "SELECT detail FROM event_log WHERE event_type = 'delivery_changed' AND memory_id = %s",
            (selected["memory_id"],),
        )
        sources = [event["detail"]["approval_source"] for event in cur.fetchall()]
    assert sources == [{"kind": "user_direct"}] * 2


def test_temporary_add_is_global_and_rejects_an_out_of_range_lifetime(dsn, monkeypatch, capsys):
    keys(monkeypatch, "p", "this should be refused", "15", "p", "frozen for the audit", "2.5", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "days must be greater than 0 and at most 14" in out
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM temporary_context")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "frozen for the audit"
    assert rows[0]["scope_id"] is None
    assert rows[0]["created_by"] == "user"


def test_temporary_context_can_be_edited_in_place(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        scope = scopes.require_scope(cur, "deployments")
        context = temporary.put_temporary(
            cur, content="the release is frozen", actor="user", days=2, scope_id=scope["scope_id"]
        )
    keys(monkeypatch, "3", "e", "the release is frozen until Friday", "-", "q")

    assert memory_ui.run(dsn) == 0

    with db.transaction(dsn) as cur:
        cur.execute(
            "SELECT content, scope_id FROM temporary_context WHERE context_id = %s",
            (context["context_id"],),
        )
        row = cur.fetchone()
    assert row["content"] == "the release is frozen until Friday"
    assert row["scope_id"] is None


def test_memory_and_temporary_context_can_be_converted_both_ways(dsn, monkeypatch):
    original = remember(dsn)
    keys(monkeypatch, "c", "2", "c", "y", "q")

    assert memory_ui.run(dsn) == 0

    with db.transaction(dsn) as cur:
        assert memories.get_memory(cur, original["memory_id"])["status"] == "retired"
        cur.execute("SELECT * FROM temporary_context")
        contexts = cur.fetchall()
        assert len(contexts) == 1 and contexts[0]["expires_at"] <= dt.datetime.now(dt.UTC)
        cur.execute("SELECT * FROM memory WHERE status = 'active'")
        active = cur.fetchall()
    assert len(active) == 1
    assert active[0]["content"] == RULE
    assert active[0]["delivery"] == "always"


@pytest.mark.parametrize(("answer", "active"), [("n", 0), ("yes", 1)])
def test_retired_conflict_shows_the_reason_and_overrides_only_after_explicit_yes(
    dsn, monkeypatch, capsys, answer, active
):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        retire(cur, memory, "the service now enforces this")
    keys(monkeypatch, "n", RULE, "", "", answer, "q")

    assert memory_ui.run(dsn) == 0
    assert "the service now enforces this" in capsys.readouterr().out
    with db.transaction(dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
        assert cur.fetchone()["n"] == active


def test_a_new_rule_can_open_a_new_topic_without_typing_an_id(dsn, monkeypatch):
    keys(
        monkeypatch,
        "n",
        RULE,
        "",
        "topic",
        "n",
        "run reports",
        "deployments",
        "Before a report",
        "q",
    )

    assert memory_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        topic = topics.require_topic(cur, "run reports")
        cur.execute("SELECT * FROM memory")
        row = cur.fetchone()
    assert topic["trigger"] == "Before a report"
    assert topic["scope_name"] == "deployments"
    assert (row["delivery"], row["topic_id"]) == ("topic", topic["topic_id"])
    assert row["scope_id"] == topic["scope_id"]


def test_the_topic_picker_lists_topics_by_number(dsn, monkeypatch, capsys):
    with db.transaction(dsn) as cur:
        topics.create_topic(cur, name="audio trim", trigger="Before cutting audio", actor="user")
        chosen = topics.create_topic(
            cur, name="difficulty", trigger="Before changing win rates", actor="user"
        )
    memory = remember(dsn)
    keys(monkeypatch, "d", "t", "2", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "1  audio trim" in out and "every session" in out and "0 rules" in out
    assert "2  difficulty" in out and "Before changing win rates" in out
    assert "n  a new topic" in out
    row = memory_row(dsn, memory["memory_id"])
    assert (row["delivery"], row["topic_id"]) == ("topic", chosen["topic_id"])


def test_a_topic_picker_answer_out_of_range_changes_nothing(dsn, monkeypatch, capsys):
    with db.transaction(dsn) as cur:
        topics.create_topic(cur, name="difficulty", trigger="Before changing", actor="user")
    memory = remember(dsn)
    keys(monkeypatch, "d", "topic", "7", "q")

    assert memory_ui.run(dsn) == 0
    assert "choose a topic number from 1 to 1" in capsys.readouterr().out
    assert memory_row(dsn, memory["memory_id"])["delivery"] == "always"


def test_a_topic_rule_shows_its_trigger_and_is_not_converted(dsn, monkeypatch, capsys):
    with db.transaction(dsn) as cur:
        topic = topics.create_topic(
            cur, name="difficulty", trigger="Before changing win rates", actor="user"
        )
    remember(dsn, delivery="topic", topic_id=topic["topic_id"])
    keys(monkeypatch, "c", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "topic:difficulty" in out
    assert "Before changing win rates" in out
    assert "topic memories cannot become temporary" in out


def test_q_quits_from_the_list_and_from_a_read_memory(dsn, monkeypatch):
    remember(dsn)
    for pressed in (("q",), ("enter", "q")):
        keys(monkeypatch, *pressed)
        with pytest.raises(screen.Quit):
            memory_ui.run.__wrapped__(dsn)


def test_help_lists_the_keys_and_q_quits_from_it(dsn, monkeypatch, capsys):
    keys(monkeypatch, "?", "x", "?", "q")
    with pytest.raises(screen.Quit):
        memory_ui.run.__wrapped__(dsn)
    out = capsys.readouterr().out
    assert out.count("Memories help") == 2
    assert "retire the selected active memory" in out
    assert out.startswith("Memories\n")


def test_columns_fit_the_values_listed_so_the_content_gets_the_rest():
    rows = [
        {"memory_id": "0123456789", "delivery": "always", "scope_name": None, "content": "x " * 60},
        {"memory_id": "abcdef0123", "delivery": "scope", "scope_name": "enrai", "content": "y"},
    ]
    columns = memory_ui._columns(rows, 79)
    assert columns == (6, 5)
    assert memory_ui._line(rows[0], "active", 79, columns).startswith(
        "   01234567  always  -      x x"
    )
    long = {
        "delivery": "topic",
        "topic_name": "a-very-long-topic-name",
        "scope_name": "a-very-long-scope-name",
    }
    assert memory_ui._columns([long], 79) == (13, 11)


@pytest.mark.usefixtures("legacy_bodies")
def test_moving_the_cursor_moves_neither_the_list_nor_the_keys(dsn, monkeypatch):
    names = ["alpha", "bravo", "charlie", "delta", "echo"]
    lengths = [1, 40, 5, 70, 15]
    for name, words in zip(names, lengths, strict=True):
        remember(dsn, f"{name} " + "check the output before reporting " * (words // 5))
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    with db.transaction(dsn) as cur:
        rows = application.memory_view(cur, "active")
    frames = [
        memory_ui._screen_text(rows, at, "active", "", total=len(rows), query="")
        for at in range(len(rows))
    ]
    assert_steady_layout(frames, names, memory_ui._KEYS)


@pytest.mark.parametrize(("answer", "active"), [("n", 0), ("yes", 1)])
def test_a_condition_repeating_a_withdrawn_rule_converts_only_after_explicit_yes(
    dsn, monkeypatch, capsys, answer, active
):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        retire(cur, memory, "the service now enforces this")
        context = temporary.put_temporary(cur, content=RULE, actor="user", days=2)
    keys(monkeypatch, "3", "c", "y", answer, "q")

    assert memory_ui.run(dsn) == 0
    assert "the service now enforces this" in capsys.readouterr().out
    with db.transaction(dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
        assert cur.fetchone()["n"] == active
        standing = [row["context_id"] for row in temporary.active_temporary(cur)]
    assert standing == ([] if active else [context["context_id"]])


@pytest.mark.parametrize(("typed", "kept"), [("", True), ("-", False)])
def test_a_rule_made_always_keeps_its_scope_unless_it_is_dropped(dsn, monkeypatch, typed, kept):
    with db.transaction(dsn) as cur:
        home = scopes.require_scope(cur, "deployments")["scope_id"]
    memory = remember(dsn, delivery="scope", scope_id=home)
    keys(monkeypatch, "d", "always", typed, "q")

    assert memory_ui.run(dsn) == 0
    row = memory_row(dsn, memory["memory_id"])
    assert (row["delivery"], row["scope_id"]) == ("always", home if kept else None)
