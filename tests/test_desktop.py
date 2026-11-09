from __future__ import annotations

import io
import json

from conftest import (
    candidate,
    expire,
    new_project,
    new_task,
    propose_change,
    remember,
    retire,
    update_task,
)
from mashu import application, db, desktop, memories, nominations, scopes, tasks

RULE = "run the migration before starting the local server, every time"


def ask(dsn: str, op: str, **args) -> dict:
    """One request through the same line loop the desktop app talks to."""
    sink = io.StringIO()
    desktop.serve(
        dsn, io.StringIO(json.dumps({"id": 1, "op": op, "args": args}, default=str) + "\n"), sink
    )
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
    return {
        row["task"]["name"]: row
        for row in ask(dsn, "work.tasks", activity="active")["result"]["tasks"]
    }


def task_status(dsn: str, task_id: str) -> str:
    with db.transaction(dsn) as cur:
        return tasks.task_get(cur, task_id)["task"]["status"]


def test_tasks_list_standing_proposals_first_and_overtaken_ones_next(dsn):
    proposed_tasks(dsn)

    rows = ask(dsn, "work.tasks", activity="active")["result"]["tasks"]

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


WITHDRAWN = "the migration now runs on its own when the server starts"


def memory_statuses(dsn: str) -> list[tuple[str, str]]:
    with db.transaction(dsn) as cur:
        cur.execute("SELECT content, status FROM memory ORDER BY created_at")
        return [(row["content"], row["status"]) for row in cur.fetchall()]


def test_writing_back_a_retired_rule_waits_until_its_retirement_is_acknowledged(dsn):
    with db.transaction(dsn) as cur:
        old = remember(cur, RULE)
        retire(cur, old, WITHDRAWN)

    refused = ask(dsn, "memory.remember", content=RULE, delivery="always")
    assert not refused["ok"]
    assert [row["retire_reason"] for row in refused["conflicts"]] == [WITHDRAWN]

    written = ask(
        dsn,
        "memory.remember",
        content=RULE,
        delivery="always",
        acknowledged=[row["memory_id"] for row in refused["conflicts"]],
    )
    assert written["ok"]
    assert memory_statuses(dsn) == [(RULE, "retired"), (RULE, "active")]


def test_a_reason_added_later_becomes_evidence_and_leaves_the_rule_as_it_was(dsn):
    with db.transaction(dsn) as cur:
        rule = remember(cur, RULE)
        before = memories.memory_details(cur, rule["memory_id"])

    why = "the server failed twice"
    assert ask(dsn, "memory.add_reason", memory_id=rule["memory_id"], why=why)["ok"]
    assert not ask(dsn, "memory.add_reason", memory_id=rule["memory_id"], why="  ")["ok"]
    too_long = ask(dsn, "memory.add_reason", memory_id=rule["memory_id"], why="x" * 501)
    assert too_long["over_limit"][0]["field"] == "why"

    with db.transaction(dsn) as cur:
        after = memories.memory_details(cur, rule["memory_id"])
    assert after["basis"][0]["what"] == "the server failed twice"
    assert after["basis"][0]["prevention"] == RULE
    assert (after["content"], after["updated_at"]) == (before["content"], before["updated_at"])


def test_a_rule_is_moved_retired_and_its_temporary_context_ended_from_the_app(dsn):
    with db.transaction(dsn) as cur:
        scopes.create_scope(cur, name="lab", actor="user")
    rule = ask(dsn, "memory.remember", content=RULE, delivery="always")["result"]

    moved = ask(dsn, "memory.delivery", memory_id=rule["memory_id"], delivery="scope", scope="lab")
    assert moved["ok"] and moved["result"]["delivery"] == "scope"

    note = ask(dsn, "temporary.add", content="the cluster is down until Friday", days=2)["result"]
    assert ask(dsn, "temporary.revise", context_id=note["context_id"], content="down", scope="lab")[
        "ok"
    ]
    retired = ask(
        dsn,
        "memory.retire",
        memory_id=rule["memory_id"],
        kind="relocated",
        reason="it only holds this week",
        destination=str(note["context_id"])[:8],
    )
    assert retired["ok"]
    assert ask(dsn, "temporary.end", context_id=note["context_id"])["ok"]

    assert memory_statuses(dsn) == [(RULE, "retired")]
    assert ask(dsn, "memory.list", view="temporary")["result"]["rows"] == []


def test_a_memory_change_applies_at_its_version_or_comes_back_reproposed(dsn):
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
        change = propose_change(
            cur, memory, "retire", retirement_kind="withdrawn", retire_reason="other repos"
        )
        memories.revise(cur, memory["memory_id"], content=f"{RULE}!", actor="user")

    stale = ask(dsn, "change.apply", change_id=change["change_id"], version=change["version"])
    assert stale["ok"] and stale["result"]["changed"]
    current = stale["result"]["current"]

    applied = ask(dsn, "change.apply", change_id=current["change_id"], version=current["version"])
    assert applied["ok"] and "applied" in applied["result"]
    assert memory_statuses(dsn) == [(f"{RULE}!", "retired")]


def test_a_change_reason_is_reworded_only_against_what_was_shown(dsn):
    with db.transaction(dsn) as cur:
        memory = remember(cur, RULE)
        change = propose_change(
            cur, memory, "retire", retirement_kind="withdrawn", retire_reason="other repos"
        )
    shown = ask(dsn, "change.list")["result"]["changes"][0]
    request = {
        "change_id": shown["change_id"],
        "target_revision_id": shown["target"]["current_revision_id"],
        "reason": "only the old repositories",
    }

    assert not ask(dsn, "change.reason", version=change["version"] + 1, **request)["ok"]
    assert ask(dsn, "change.reason", version=shown["version"], **request)["ok"]
    assert ask(dsn, "change.list")["result"]["changes"][0]["retire_reason"] == request["reason"]


def test_a_task_that_reads_like_an_open_one_is_created_only_when_forced(dsn):
    with db.transaction(dsn) as cur:
        new_project(cur)
        new_task(cur, "wire the tray icon")
    request = {"name": "wire the tray icon", "project": "enrai"}

    refused = ask(dsn, "task.create", **request)
    assert not refused["ok"]
    assert [row["task"]["name"] for row in refused["candidates"]] == ["wire the tray icon"]
    assert ask(dsn, "task.create", force=True, **request)["ok"]
    assert len(listed_tasks(dsn, "active")) == 2


def listed_tasks(dsn: str, activity: str) -> list[dict]:
    return ask(dsn, "work.tasks", activity=activity)["result"]["tasks"]


def test_a_task_edit_against_an_old_state_is_refused_and_a_closed_task_reopens(dsn):
    with db.transaction(dsn) as cur:
        new_project(cur)
        task_id = str(new_task(cur, "wire the tray")["task"]["task_id"])
    shown = listed_tasks(dsn, "active")[0]
    edit = {
        "task_id": task_id,
        "expect_updated_at": shown["state"]["updated_at"],
        "name": "wire the tray",
        "project": "enrai",
        "goal": "a tray icon on Windows",
        "next_actions": ["draw the icon", " "],
    }

    assert ask(dsn, "task.update", **edit)["ok"]
    assert not ask(dsn, "task.update", **{**edit, "goal": "late"})["ok"]
    state = listed_tasks(dsn, "active")[0]["state"]
    assert (state["goal"], state["next_actions"]) == ("a tray icon on Windows", ["draw the icon"])

    assert ask(dsn, "task.close", task_id=task_id, outcome="completed")["ok"]
    assert [row["task"]["name"] for row in listed_tasks(dsn, "closed")] == ["wire the tray"]
    assert ask(dsn, "task.reopen", task_id=task_id)["ok"]
    assert listed_tasks(dsn, "closed") == []


def test_projects_scopes_topics_and_routes_are_kept_from_the_settings_pages(dsn):
    assert ask(dsn, "scope.save", name="lab", summary="the lab machines")["ok"]
    assert ask(dsn, "project.save", name="tray", scope="lab")["ok"]
    project = ask(dsn, "work.projects")["result"]["projects"][0]
    assert ask(dsn, "project.save", project_id=project["project_id"], name="tray app")["ok"]

    topic = ask(dsn, "topic.save", name="deploy", trigger="Before deploying", scope="lab")
    assert topic["ok"]
    edited = ask(
        dsn,
        "topic.save",
        topic_id=topic["result"]["topic_id"],
        name="deploy",
        trigger="Before deploying anything",
        action="deploy",
    )
    assert edited["ok"]
    listed_topic = ask(dsn, "topic.list")["result"]["topics"][0]
    assert (listed_topic["scope_name"], listed_topic["action"]) == (None, "deploy")
    assert ask(dsn, "topic.remove", topic_id=listed_topic["topic_id"])["result"]["removed"]

    assert ask(dsn, "route.save", path="/work/lab", scope="lab")["ok"]
    assert ask(dsn, "route.save", path="/work/scratch")["ok"]
    routes = {
        row["path_prefix"]: row["scope_name"] for row in ask(dsn, "route.list")["result"]["routes"]
    }
    assert routes == {"/work/lab": "lab", "/work/scratch": None}
    assert ask(dsn, "route.remove", path="/work/scratch")["result"]["removed"]

    assert [row["name"] for row in ask(dsn, "work.projects")["result"]["projects"]] == ["tray app"]


def test_the_checkpoint_interval_keeps_to_its_bounds_and_health_reads_without_writing(dsn):
    assert ask(dsn, "interval.set", minutes=15)["result"]["minutes"] == 15
    assert not ask(dsn, "interval.set", minutes=-1)["ok"]
    assert ask(dsn, "interval.get")["result"]["minutes"] == 15

    health = ask(dsn, "settings.health")["result"]
    assert health["capacities"]["always"] > 0
    assert health["missed_30d"] == 0
    assert ask(dsn, "schema.status")["result"]["pending"] == []
    preview = ask(dsn, "settings.bootstrap", cwd="/nowhere/in/particular")
    assert preview["ok"] and preview["result"]["scope"] is None
