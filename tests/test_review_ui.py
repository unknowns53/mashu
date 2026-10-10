from __future__ import annotations

import pathlib
import sys

import pytest

from conftest import candidate, keys, read_through, remember, retire
from mashu import db, nominations, review_ui, screen, topics

RULE = "run the migration before starting the local server, every time"
OTHER = "spell out the timezone in every scheduled job, even when it looks obvious"

pytestmark = pytest.mark.usefixtures("known_screen")


def a_candidate(dsn: str, content: str) -> dict:
    with db.transaction(dsn) as cur:
        return candidate(cur, content)


def a_topic(dsn: str, name: str = "migrations") -> dict:
    with db.transaction(dsn) as cur:
        return topics.create_topic(cur, name=name, trigger="Before a migration", actor="user")


def nomination_row(dsn: str, nomination_id) -> dict:
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM nomination WHERE nomination_id = %s", (nomination_id,))
        return cur.fetchone()


def memory_rows(dsn: str) -> list[dict]:
    with db.transaction(dsn) as cur:
        cur.execute("SELECT * FROM memory ORDER BY created_at")
        return cur.fetchall()


@pytest.mark.usefixtures("legacy_bodies")
def test_stale_candidate_shows_new_short_wording_from_the_first_page(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, "\n".join(f"old wording {n}" for n in range(30)))
    row = review_ui._queue(dsn, False)[0][0]
    monkeypatch.setenv("LINES", "12")
    read_through(monkeypatch, review_ui._item_text(row, 1, 1), review_ui._ITEM_KEYS, "y", "", "q")
    getkey = screen.getkey

    def change_before_admission():
        key = getkey()
        if key == "y":
            with db.transaction(dsn) as cur:
                nominations.revise(
                    cur, nomination["nomination_id"], content="new short wording", actor="user"
                )
        return key

    monkeypatch.setattr(screen, "getkey", change_before_admission)
    review_ui.run(dsn)
    assert "new short wording" in capsys.readouterr().out
    assert nomination_row(dsn, nomination["nomination_id"])["status"] == "pending"


@pytest.mark.usefixtures("legacy_bodies")
def test_admission_acknowledges_conflict_only_after_its_reason_is_shown(dsn, monkeypatch):
    body = "\n".join(f"check archive index step {n}" for n in range(30))
    with db.transaction(dsn) as cur:
        conflict = remember(cur, body)
        retire(cur, conflict, "unsafe now")
        nomination = candidate(cur, body, conflicts=[conflict["memory_id"]])
    monkeypatch.setenv("LINES", "12")
    keys(monkeypatch, "", "y", "", "q")
    review_ui.run(dsn)
    assert nomination_row(dsn, nomination["nomination_id"])["status"] == "pending"

    row = review_ui._queue(dsn, False)[0][0]
    read_through(monkeypatch, review_ui._item_text(row, 1, 1), review_ui._ITEM_KEYS, "y", "")
    review_ui.run(dsn)
    approved = nomination_row(dsn, nomination["nomination_id"])
    assert approved["status"] == "admitted"
    assert approved["approval_source"]["conflict_ids"] == [str(conflict["memory_id"])]
    assert approved["approval_source"]["conflict_acknowledged_by_action"] is True


def test_ctrl_c_at_admission_delivery_keeps_the_candidate_pending(dsn, monkeypatch):
    nomination = a_candidate(dsn, RULE)

    def cancel(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)

    review_ui._admit(dsn, nomination)
    assert nomination_row(dsn, nomination["nomination_id"])["status"] == "pending"
    assert memory_rows(dsn) == []


def test_putting_one_off_hides_it_until_all_asks_for_it_with_its_reason(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, RULE)
    reason = "waiting on the other team to answer"
    keys(monkeypatch, "", "s", reason)

    assert review_ui.run(dsn) == 0

    assert "--all to see them" in capsys.readouterr().out
    row = nomination_row(dsn, nomination["nomination_id"])
    assert row["status"] == "pending"
    assert row["deferred_at"] is not None
    assert row["defer_reason"] == reason

    keys(monkeypatch, "q")
    assert review_ui.run(dsn, show_deferred=True) == 0
    out = capsys.readouterr().out
    assert f"preview  {str(nomination['nomination_id'])[:8]}" in out
    assert RULE in out
    assert f"put off: {reason}" in out


def test_search_matches_every_documented_field_case_insensitively():
    row = {
        "nomination_id": "AbCd-1234",
        "kind": "User_Explicit",
        "scope_name": "Deployments",
        "content": "Keep the standby warm",
        "evidence_rows": [{"what": "The RELEASE stalled", "prevention": "Check the Canary first"}],
    }

    for query in ("abcd", "EXPLICIT", "deploy", "STANDBY", "release", "canary"):
        assert review_ui._matches_query(row, query)
    assert not review_ui._matches_query(row, "unrelated")


def test_zero_result_search_can_be_researched_and_empty_search_restores_the_queue(
    dsn, monkeypatch, capsys
):
    first = a_candidate(dsn, RULE)
    second = a_candidate(dsn, OTHER)
    keys(monkeypatch, "/", "does not exist", "/", "TIMEZONE", "", "y", "", "/", "", "q")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "no candidates match" in out
    assert nomination_row(dsn, first["nomination_id"])["status"] == "pending"
    assert nomination_row(dsn, second["nomination_id"])["status"] == "admitted"
    assert "1 waiting for review" in out


@pytest.mark.parametrize(
    ("start", "key", "expected"),
    [
        (1, "j", 2),
        (1, "down", 2),
        (1, "k", 0),
        (1, "up", 0),
        (2, "home", 0),
        (0, "end", 4),
        (4, "pageup", 0),
        (0, "pagedown", 4),
    ],
)
def test_candidate_navigation_is_shared_by_queue_and_item(start, key, expected):
    assert review_ui._move(start, 5, key) == expected


@pytest.mark.parametrize(
    ("movement", "decided"),
    [
        (("j", "", "k", "j"), 1),
        (("end", "", "home", "end", "pageup", "pagedown"), 2),
    ],
)
def test_queue_and_item_use_the_same_candidate_navigation(dsn, monkeypatch, movement, decided):
    candidates = [
        a_candidate(dsn, "run migrations before the local server"),
        a_candidate(dsn, "write explicit timezones in scheduler entries"),
        a_candidate(dsn, "check the standby before deploying"),
    ]
    keys(monkeypatch, *movement, "r", "no longer needed", "q")

    assert review_ui.run(dsn) == 0

    rows = [nomination_row(dsn, row["nomination_id"]) for row in candidates]
    assert [row["status"] for row in rows].count("pending") == 2
    assert rows[decided]["status"] == "declined"
    assert rows[decided]["decision_reason"] == "no longer needed"


def test_the_queue_names_every_candidate_and_left_clears_a_search_first(dsn, monkeypatch, capsys):
    first = a_candidate(dsn, RULE)
    second = a_candidate(dsn, OTHER)
    keys(monkeypatch, "/", "timezone", "left", "q")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    searched = "1 of 2 waiting for review  ·  search: timezone"
    assert searched in out
    before_search = out[: out.index(searched)]
    assert str(first["nomination_id"])[:8] in before_search
    assert str(second["nomination_id"])[:8] in before_search
    assert out.count("2 waiting for review") >= 2


def test_a_capacity_refusal_keeps_the_candidate_and_can_be_edited_inline_and_retried(
    dsn, monkeypatch, capsys
):
    candidate = a_candidate(dsn, RULE)
    monkeypatch.setenv("MASHU_CAPACITY", "12")
    monkeypatch.setenv("MASHU_ALWAYS_CAPACITY", "12")
    keys(monkeypatch, "", "y", "", "q")

    assert review_ui.run(dsn) == 0
    assert "seats 12 tokens" in capsys.readouterr().out
    assert memory_rows(dsn) == []
    assert nomination_row(dsn, candidate["nomination_id"])["status"] == "pending"

    keys(monkeypatch, "", "y", "", "e", "migrate first", "y", "")
    assert review_ui.run(dsn) == 0
    assert memory_rows(dsn)[0]["content"] == "migrate first"
    assert nomination_row(dsn, candidate["nomination_id"])["status"] == "admitted"


def test_editing_persists_the_candidate_then_it_can_be_admitted(dsn, monkeypatch, tmp_path):
    candidate = a_candidate(dsn, RULE)
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
    keys(monkeypatch, "", "e", "y", "")

    assert review_ui.run(dsn) == 0

    rows = memory_rows(dsn)
    assert len(rows) == 1
    assert rows[0]["content"].startswith(RULE)
    assert "and check the standby first" in rows[0]["content"]
    assert rows[0]["delivery"] == "always"
    saved = nomination_row(dsn, candidate["nomination_id"])
    assert saved["content"] == rows[0]["content"]


def test_a_candidate_that_walks_back_a_retirement_says_so(dsn, monkeypatch, capsys):
    withdrawn = "the server runs the migration on boot now"
    with db.transaction(dsn) as cur:
        kept = remember(cur, OTHER)
        retire(cur, kept, withdrawn)
        candidate(cur, RULE, conflicts=[kept["memory_id"]])
    keys(monkeypatch, "", "q")

    assert review_ui.run(dsn) == 0

    out = capsys.readouterr().out
    assert "retired conflict" in out
    assert "conflicts: 1" in out
    assert withdrawn in out
    assert OTHER not in out


@pytest.mark.parametrize("answer", ["t migrations", "t"])
def test_admission_files_under_a_topic_named_or_picked(dsn, monkeypatch, capsys, answer):
    a_topic(dsn, "audio")
    chosen = a_topic(dsn, "migrations")
    a_candidate(dsn, RULE)
    keys(monkeypatch, "", "y", answer, "2")

    assert review_ui.run(dsn) == 0

    assert "'t NAME' for a topic" in capsys.readouterr().out
    rows = memory_rows(dsn)
    assert (rows[0]["delivery"], rows[0]["topic_id"]) == ("topic", chosen["topic_id"])


def test_an_unknown_topic_at_admission_keeps_the_candidate_pending(dsn, monkeypatch, capsys):
    nomination = a_candidate(dsn, RULE)
    keys(monkeypatch, "", "y", "t nowhere", "q")

    assert review_ui.run(dsn) == 0
    assert "no topic named 'nowhere'" in capsys.readouterr().out
    assert nomination_row(dsn, nomination["nomination_id"])["status"] == "pending"
