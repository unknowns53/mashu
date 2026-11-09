from __future__ import annotations

import io
import json

from conftest import candidate, expire, new_project, new_task, update_task
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


def proposed_tasks(dsn: str) -> dict[str, str]:
    """Three open tasks: one plain, one proposed, one proposed and then written again."""
    with db.transaction(dsn) as cur:
        new_project(cur)
        ids = {
            name: new_task(cur, name, force=True)["task"]["task_id"]
            for name in ("plain", "proposed", "overtaken")
        }
        for name in ("proposed", "overtaken"):
            tasks.propose_close(
                cur, ids[name], outcome="completed", reason="it runs", actor="agent"
            )
        update_task(cur, ids["overtaken"], status_text="one more fix")
    return {name: str(task_id) for name, task_id in ids.items()}


def listed(dsn: str) -> dict[str, dict]:
    return {row["task"]["name"]: row for row in ask(dsn, "task.list")["result"]["tasks"]}


def task_status(dsn: str, task_id: str) -> str:
    with db.transaction(dsn) as cur:
        return tasks.task_get(cur, task_id)["task"]["status"]


def test_tasks_list_standing_proposals_first_and_overtaken_ones_next(dsn):
    proposed_tasks(dsn)

    rows = ask(dsn, "task.list")["result"]["tasks"]

    assert [row["task"]["name"] for row in rows] == ["proposed", "overtaken", "plain"]
    assert [row["proposal"] and row["proposal"]["stale"] for row in rows] == [False, True, None]


def test_a_proposal_is_accepted_only_as_it_was_shown(dsn):
    ids = proposed_tasks(dsn)
    shown = listed(dsn)
    with db.transaction(dsn) as cur:
        update_task(cur, ids["proposed"], status_text="written after the list was read")

    refused = ask(
        dsn,
        "task.accept",
        task_id=ids["proposed"],
        proposal=shown["proposed"]["proposal"],
        state_at=shown["proposed"]["state"]["updated_at"],
    )
    assert not refused["ok"] and "changed" in refused["error"]
    assert task_status(dsn, ids["proposed"]) == "open"

    current = listed(dsn)["proposed"]
    accepted = ask(
        dsn,
        "task.accept",
        task_id=ids["proposed"],
        proposal=current["proposal"],
        state_at=current["state"]["updated_at"],
    )
    assert accepted["ok"]
    assert task_status(dsn, ids["proposed"]) == "closed"


def test_a_task_is_closed_kept_open_or_renewed_on_the_persons_word(dsn):
    ids = proposed_tasks(dsn)
    with db.transaction(dsn) as cur:
        expire(cur, ids["plain"])

    assert ask(dsn, "task.close", task_id=ids["overtaken"], outcome="abandoned", reason=" ")["ok"]
    assert ask(dsn, "task.keep", task_id=ids["proposed"])["ok"]
    assert not ask(dsn, "task.keep", task_id=ids["plain"])["ok"]
    assert ask(dsn, "task.renew", task_id=ids["plain"])["ok"]
    assert not ask(dsn, "task.close", task_id=ids["plain"], outcome="finished")["ok"]

    rows = listed(dsn)
    assert task_status(dsn, ids["overtaken"]) == "closed"
    assert rows["proposed"]["proposal"] is None
    assert (rows["proposed"]["activity"], rows["plain"]["activity"]) == ("active", "active")


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
