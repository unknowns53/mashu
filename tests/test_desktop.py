from __future__ import annotations

import io
import json

from conftest import candidate, new_project, new_task
from mashu import application, db, desktop, nominations, tasks

RULE = "run the migration before starting the local server, every time"


def ask(dsn: str, op: str, **args) -> dict:
    """One request through the same line loop the desktop app talks to."""
    sink = io.StringIO()
    desktop.serve(dsn, io.StringIO(json.dumps({"id": 1, "op": op, "args": args}) + "\n"), sink)
    lines = sink.getvalue().splitlines()
    assert len(lines) == 1
    reply = json.loads(lines[0])
    assert reply["id"] == 1
    return reply


def a_candidate(dsn: str, content: str = RULE) -> dict:
    with db.transaction(dsn) as cur:
        return candidate(cur, content)


def test_a_candidate_listed_then_admitted_at_its_version_becomes_an_active_memory(dsn):
    made = a_candidate(dsn)
    listed = ask(dsn, "review.list")["result"]["candidates"]
    assert [row["content"] for row in listed] == [RULE]

    reply = ask(
        dsn,
        "review.admit",
        nomination_id=listed[0]["nomination_id"],
        expected_version=listed[0]["version"],
        delivery="always",
    )

    assert reply["ok"] and "admitted" in reply["result"]
    with db.transaction(dsn) as cur:
        cur.execute("SELECT status, content FROM memory")
        assert cur.fetchall() == [{"status": "active", "content": RULE}]
        cur.execute(
            "SELECT status FROM nomination WHERE nomination_id = %s", (made["nomination_id"],)
        )
        assert cur.fetchone()["status"] == "admitted"
    assert ask(dsn, "status")["result"]["pending_ready"] == 0


def test_admitting_a_version_already_replaced_hands_back_the_current_wording(dsn):
    made = a_candidate(dsn)
    revised = ask(
        dsn, "review.revise", nomination_id=str(made["nomination_id"]), content=f"{RULE}!"
    )
    assert revised["ok"]

    reply = ask(
        dsn,
        "review.admit",
        nomination_id=str(made["nomination_id"]),
        expected_version=made["version"],
        delivery="always",
    )

    assert reply["ok"]
    assert reply["result"]["changed"] == "candidate"
    assert reply["result"]["current"]["content"] == f"{RULE}!"
    with db.transaction(dsn) as cur:
        assert [row["content"] for row in nominations.pending_nominations(cur)] == [f"{RULE}!"]


def test_declining_and_deferring_take_a_reason_and_leave_the_ready_queue(dsn):
    declined, deferred = a_candidate(dsn), a_candidate(dsn, "spell out the timezone every time")

    assert not ask(dsn, "review.decline", nomination_id=str(declined["nomination_id"]))["ok"]
    for op, made in (("review.decline", declined), ("review.defer", deferred)):
        assert ask(dsn, op, nomination_id=str(made["nomination_id"]), reason="not yet")["ok"]

    status = ask(dsn, "status")["result"]
    assert (status["pending_ready"], status["pending_deferred"]) == (0, 1)
    assert ask(dsn, "review.list")["result"]["hidden_deferred"] == 1


def test_status_names_the_tasks_an_agent_proposed_to_close(dsn):
    with db.transaction(dsn) as cur:
        new_project(cur)
        task = new_task(cur, "wire the tray")
        tasks.propose_close(
            cur, task["task"]["task_id"], outcome="completed", reason="it runs", actor="agent"
        )

    proposals = ask(dsn, "status")["result"]["close_proposals"]

    assert [(row["name"], row["outcome"]) for row in proposals] == [("wire the tray", "completed")]


def test_a_read_cannot_write_even_if_the_code_behind_it_tries(dsn, monkeypatch):
    snapshot = application.status_snapshot

    def writing_snapshot(cur):
        candidate(cur, "slipped in through a read")
        return snapshot(cur)

    monkeypatch.setattr(application, "status_snapshot", writing_snapshot)

    reply = ask(dsn, "status")

    assert not reply["ok"] and reply["error"].startswith("database:")
    with db.transaction(dsn) as cur:
        assert nominations.pending_nominations(cur) == []


def test_requests_that_cannot_be_carried_out_are_answered_on_their_own_line(dsn):
    sink = io.StringIO()
    lines = [
        "not json",
        "\n",
        json.dumps({"id": 2, "op": "review.drop"}),
        json.dumps({"id": 3, "op": "review.admit", "args": {"nomination_id": "nope"}}),
    ]
    desktop.serve(dsn, io.StringIO("\n".join(lines) + "\n"), sink)

    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [(reply["id"], reply["ok"]) for reply in replies] == [
        (None, False),
        (2, False),
        (3, False),
    ]
    assert "unknown op" in replies[1]["error"]
    assert "delivery" in replies[2]["error"]
