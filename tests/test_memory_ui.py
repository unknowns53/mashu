from __future__ import annotations

import io
import os

import psycopg
import pytest

from mashu import db, memories, memory_ui, scopes, temporary
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_memory_ui"

RULE = "never report a run as finished without the output that proves it"
REVISED = "never report a run as finished without pasting the output that proves it"


@pytest.fixture
def dsn() -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    conninfo = f"dbname={TEST_DB}"
    migrate(conninfo)
    with db.transaction(conninfo) as cur:
        scopes.create_scope(cur, name="deployments", actor="user")
    return conninfo


@pytest.fixture(autouse=True)
def a_screen_of_a_known_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "40")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


def keys(monkeypatch: pytest.MonkeyPatch, *typed: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in typed)))


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
        memories.retire(
            cur,
            retired["memory_id"],
            reason="the deployer enforces this now",
            actor="user",
        )
        scope = scopes.require_scope(cur, "deployments")
        context = temporary.put_temporary(
            cur,
            content="the canary is paused while the incident is open",
            actor="user",
            days=2,
            scope_id=scope["scope_id"],
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


def test_full_detail_explains_evidence_and_revision_history(dsn, monkeypatch, capsys):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        memories.revise(
            cur,
            memory["memory_id"],
            content=REVISED,
            actor="user",
            note="say what evidence must be pasted",
        )
    keys(monkeypatch, "enter", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "evidence" in out
    assert "explicit" in out
    assert "recorded by the user's own hand" in out
    assert f"prevention: {RULE}" in out
    assert "revisions" in out
    assert RULE in out and REVISED in out
    assert "note: say what evidence must be pasted" in out
    assert "by user" in out


def test_long_detail_can_be_paged_forward_and_back(dsn, monkeypatch, capsys):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        for number in range(8):
            memories.revise(
                cur,
                memory["memory_id"],
                content=f"revision wording number {number}",
                actor="user",
                note=f"revision note number {number}",
            )
    monkeypatch.setenv("LINES", "12")
    keys(monkeypatch, "enter", *("space" for _ in range(12)), "b", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "more line(s), space to go on" in out
    assert "revision note number 7" in out


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


def test_new_durable_memory_can_choose_a_scope_delivery(dsn, monkeypatch):
    keys(monkeypatch, "n", RULE, "scope", "deployments", "q")

    assert memory_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM memory")
        row = cur.fetchone()
        scope = scopes.require_scope(cur, "deployments")
    assert row["content"] == RULE
    assert row["delivery"] == "scope"
    assert row["scope_id"] == scope["scope_id"]
    assert row["created_by"] == "user"


def test_revision_without_an_editor_uses_safe_line_input(dsn, monkeypatch, capsys):
    memory = remember(dsn)
    keys(monkeypatch, "e", REVISED, "q")

    assert memory_ui.run(dsn) == 0
    assert memory_row(dsn, memory["memory_id"])["content"] == REVISED
    assert f"revised {str(memory['memory_id'])[:8]}" in capsys.readouterr().out


def test_retirement_requires_a_reason_and_confirmation(dsn, monkeypatch):
    memory = remember(dsn)
    keys(monkeypatch, "r", "", "r", "the runner checks this itself", "n", "r", "reason", "y", "q")

    assert memory_ui.run(dsn) == 0
    row = memory_row(dsn, memory["memory_id"])
    assert row["status"] == "retired"
    assert row["retire_reason"] == "reason"


def test_delivery_can_be_changed_to_a_global_guard(dsn, monkeypatch):
    memory = remember(dsn)
    keys(monkeypatch, "d", "guard", "Bash", "", "q")

    assert memory_ui.run(dsn) == 0
    row = memory_row(dsn, memory["memory_id"])
    assert row["delivery"] == "guard"
    assert row["guard_action"] == "Bash"
    assert row["scope_id"] is None


def test_delivery_edit_keeps_existing_guard_defaults_on_empty_input(dsn, monkeypatch):
    with db.transaction(dsn) as cur:
        scope = scopes.require_scope(cur, "deployments")
        memory = memories.remember(
            cur,
            content=RULE,
            actor="user",
            delivery="guard",
            guard_action="Bash",
            scope_id=scope["scope_id"],
        )
    keys(monkeypatch, "d", "", "", "", "q")

    assert memory_ui.run(dsn) == 0
    row = memory_row(dsn, memory["memory_id"])
    assert row["delivery"] == "guard"
    assert row["guard_action"] == "Bash"
    assert row["scope_id"] == scope["scope_id"]


def test_selection_stays_on_the_same_id_when_a_delivery_change_reorders_the_list(dsn, monkeypatch):
    selected = remember(dsn)
    other = remember(dsn, "spell out the timezone in scheduled jobs")
    keys(monkeypatch, "d", "guard", "Bash", "", "e", REVISED, "q")

    assert memory_ui.run(dsn) == 0
    assert memory_row(dsn, selected["memory_id"])["content"] == REVISED
    assert memory_row(dsn, other["memory_id"])["content"] == (
        "spell out the timezone in scheduled jobs"
    )


def test_temporary_add_is_global_and_rejects_an_out_of_range_lifetime(dsn, monkeypatch, capsys):
    keys(
        monkeypatch,
        "p",
        "this should be refused",
        "15",
        "p",
        "the release is frozen for the audit",
        "2.5",
        "q",
    )

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "days must be greater than 0 and at most 14" in out
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM temporary_context")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "the release is frozen for the audit"
    assert rows[0]["scope_id"] is None
    assert rows[0]["created_by"] == "user"


def test_retired_conflict_shows_the_reason_and_does_not_override_without_yes(
    dsn, monkeypatch, capsys
):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        memories.retire(
            cur,
            memory["memory_id"],
            reason="the service now enforces this",
            actor="user",
        )
    keys(monkeypatch, "n", RULE, "", "n", "q")

    assert memory_ui.run(dsn) == 0
    out = capsys.readouterr().out
    assert "the service now enforces this" in out
    assert "retirement kept" in out
    with db.transaction(dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
        assert cur.fetchone()["n"] == 0


def test_retired_conflict_is_overridden_only_after_explicit_yes(dsn, monkeypatch):
    memory = remember(dsn)
    with db.transaction(dsn) as cur:
        memories.retire(cur, memory["memory_id"], reason="old advice", actor="user")
    keys(monkeypatch, "n", RULE, "", "yes", "q")

    assert memory_ui.run(dsn) == 0
    with db.transaction(dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM memory WHERE status = 'active'")
        assert cur.fetchone()["n"] == 1


def test_non_tty_rendering_contains_no_ansi(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    row = {
        "memory_id": "12345678-abcd",
        "content": RULE,
        "scope_name": None,
        "delivery": "guard",
        "guard_action": "Bash",
    }

    rendered = memory_ui._screen_text([row], 0, "active", "", total=1, query="")

    assert "\x1b[" not in rendered
    assert RULE in rendered
    assert "guard" in rendered and "Bash" in rendered
