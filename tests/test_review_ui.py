
from __future__ import annotations

import io
import os
import pathlib
import sys

import psycopg
import pytest

from mashu import db, ledger, nominations, review_ui
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")
TEST_DB = f"{os.environ.get('MASHU_TEST_DB', 'mashu_test')}_review"

RULE = "run the migration before starting the local server, every time"
OTHER = "spell out the timezone in every scheduled job, even when it looks obvious"


@pytest.fixture
def dsn() -> str:
    """A database of this test's own, built from the migrations."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    conninfo = f"dbname={TEST_DB}"
    migrate(conninfo)
    return conninfo


@pytest.fixture(autouse=True)
def a_screen_of_a_known_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal the paging can be reasoned about, whatever runs the suite."""
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "40")


def keys(monkeypatch: pytest.MonkeyPatch, *typed: str) -> None:
    """Hand the sitting its keystrokes: one line each, in the order pressed."""
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in typed)))


def a_candidate(dsn: str, content: str) -> dict:
    """One candidate standing on one recorded pain, committed."""
    with db.transaction(dsn) as cur:
        pain = ledger.report_pain(
            cur,
            kind="friction",
            what="looked it up again",
            prevention=content,
            actor="agent",
        )
        return nominations.create_nomination(
            cur,
            content=content,
            kind="rederivation",
            evidence=[pain["ledger_id"]],
            actor="agent",
        )


def nomination_row(dsn: str, nomination_id) -> dict:
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM nomination WHERE nomination_id = %s", (nomination_id,))
        return cur.fetchone()


def memory_rows(dsn: str) -> list[dict]:
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM memory ORDER BY created_at")
        return cur.fetchall()


def test_opening_a_candidate_and_pressing_y_admits_it_where_it_belongs(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, RULE)
    keys(monkeypatch, "", "y", "")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "nothing waiting for review" in out
    rows = memory_rows(dsn)
    assert len(rows) == 1
    assert rows[0]["content"] == RULE
    assert rows[0]["status"] == "active"
    assert rows[0]["delivery"] == "always"
    assert nomination_row(dsn, nomination["nomination_id"])["status"] == "admitted"


def test_turning_one_down_keeps_the_reason_that_was_typed_for_it(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, RULE)
    keys(monkeypatch, "", "r", "the tool refuses on its own now")

    assert review_ui.run(dsn) == 0

    capsys.readouterr()
    row = nomination_row(dsn, nomination["nomination_id"])
    assert row["status"] == "declined"
    assert row["decision_reason"] == "the tool refuses on its own now"
    assert memory_rows(dsn) == []


def test_putting_one_off_hides_it_from_the_sitting_until_all_asks_for_it(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, RULE)
    keys(monkeypatch, "", "s", "waiting on the other team to answer")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "nothing waiting for review" in out
    assert "--all to see them" in out
    row = nomination_row(dsn, nomination["nomination_id"])
    assert row["status"] == "pending"
    assert row["deferred_at"] is not None
    assert row["defer_reason"] == "waiting on the other team to answer"

    with db.transaction(dsn) as cur:
        assert nominations.pending_nominations(cur, include_deferred=False) == []
        assert len(nominations.pending_nominations(cur)) == 1

    keys(monkeypatch, "q")
    assert review_ui.run(dsn, show_deferred=True) == 0
    assert "1 waiting for review" in capsys.readouterr().out


def test_a_refused_admission_leaves_the_reader_on_the_same_candidate(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, RULE)
    monkeypatch.setenv("MASHU_CAPACITY", "1")
    keys(monkeypatch, "", "y", "", "q")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "the opening seats 1 tokens" in out
    assert memory_rows(dsn) == []
    assert nomination_row(dsn, nomination["nomination_id"])["status"] == "pending"


def test_editing_before_admitting_keeps_the_wording_that_was_written(
    dsn, monkeypatch, capsys, tmp_path
):
    a_candidate(dsn, RULE)
    # Run the editor through Python; Windows cannot execute a shebang script here.
    editor = tmp_path / "append_a_line.py"
    editor.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.write_text(path.read_text(encoding='utf-8')"
        " + 'and check the standby first\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    interpreter = pathlib.Path(sys.executable).as_posix()
    monkeypatch.setenv("EDITOR", f'"{interpreter}" "{editor.as_posix()}"')
    monkeypatch.delenv("VISUAL", raising=False)
    keys(monkeypatch, "", "e", "")

    assert review_ui.run(dsn) == 0

    capsys.readouterr()
    rows = memory_rows(dsn)
    assert len(rows) == 1
    assert rows[0]["content"].startswith(RULE)
    assert "and check the standby first" in rows[0]["content"]


def test_the_queue_names_every_candidate_before_any_of_them_is_opened(dsn, monkeypatch, capsys):
    first = a_candidate(dsn, RULE)
    second = a_candidate(dsn, OTHER)
    keys(monkeypatch, "q")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "2 waiting for review" in out
    assert str(first["nomination_id"])[:8] in out
    assert str(second["nomination_id"])[:8] in out


def test_a_candidate_that_walks_back_a_retirement_says_so_above_its_evidence(
    dsn, monkeypatch, capsys
):
    withdrawn = "the server runs the migration on boot now"
    with db.transaction(dsn) as cur:
        from mashu import memories

        kept = memories.remember(cur, content=OTHER, actor="user")
        memories.retire(cur, kept["memory_id"], reason=withdrawn, actor="user")
        # Keep the existing candidate so the repeated pain carries its conflict.
        pain = ledger.report_pain(
            cur, kind="friction", what="looked it up", prevention=RULE, actor="agent"
        )
        nominations.create_nomination(
            cur,
            content=RULE,
            kind="user_explicit",
            evidence=[pain["ledger_id"]],
            actor="agent",
            conflicts=[kept["memory_id"]],
        )
    keys(monkeypatch, "", "q")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "contradicts a retired memory" in out
    assert withdrawn in out
    assert OTHER not in out
    assert out.index("contradicts a retired memory") < out.index("evidence")
