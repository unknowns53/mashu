from __future__ import annotations

import argparse
import json
from uuid import UUID, uuid4

import pytest

from conftest import propose_change, remember
from mashu import (
    cli,
    config,
    db,
    memories,
    nominations,
    projects,
    task_history,
    tasks,
)
from mashu import traces as trace_domain

# Keep test strings dissimilar to prevent cross-test trigram matches.
RULE = "keep the deployment order in the runbook rather than in anyone's head"
NAMED = "hold the terminal wide open while a long import prints, or watch it wrap badly"
TWIN = "wipe the scratch directory between two runs of the packager, always"
EVIDENCED = "apply a migration on the standby first; the primary is not the rehearsal"
LISTED = "spell out the timezone in every scheduled job, even when it looks obvious"
SCOPED = "reach for the vendored toolchain in this checkout, not whatever is on PATH"
DEFERRED = "name the branch after the ticket, so a stale checkout tells on itself"
CURRENT_WORK = "wire the project state into the opening"

#: A UUID prefix cannot contain twelve consecutive hex digits; position nine is a dash.
NOWHERE = "ffffffffffff"

#: Two ids that agree for four characters and part at the fifth.
TWIN_A = "abcd0001-0000-4000-8000-00000000000a"
TWIN_B = "abcd0002-0000-4000-8000-00000000000b"


@pytest.fixture
def run(committing_dsn, capsys):
    """Call the CLI as a person would, and hand back its exit code and output."""

    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(["--dsn", committing_dsn, *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def pain(run, prevention: str, kind: str = "incident", what: str = "it went wrong"):
    return run("pain", "--kind", kind, "--what", what, "--prevention", prevention)


def remembered_id(out: str) -> str:
    assert out.startswith("remembered  "), out
    return out.split()[1]


def pending(out: str, content: str) -> tuple[str, int]:
    """The nomination id and version shown with this proposed body in `review --list`."""
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == content and index and not lines[index - 1].startswith(" "):
            nomination_id, version = lines[index - 1].split()[:2]
            return nomination_id, int(version.removeprefix("v"))
    raise AssertionError(f"no pending nomination proposing {content!r} in:\n{out}")


def command_paths(parser, path=()):
    """Every command and subcommand the parser accepts, walked from the parser itself."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield (*path, name)
                yield from command_paths(child, (*path, name))


@pytest.mark.parametrize("path", tuple(command_paths(cli.build_parser())), ids="-".join)
def test_every_command_help_has_a_description_and_an_example(path, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([*path, "--help"])

    out, err = capsys.readouterr()
    assert stopped.value.code == 0 and err == ""
    assert f"usage: mashu {' '.join(path)}" in out
    assert "examples:\n  mashu " in out


def test_serve_uses_the_explicit_dsn_for_server_transactions(committing_dsn, monkeypatch):
    from mashu import server

    monkeypatch.setenv("MASHU_DATABASE_URL", "dbname=not_the_selected_store")
    seen = []

    def server_main():
        with db.transaction() as cur:
            cur.execute("SELECT current_database() AS name")
            seen.append(cur.fetchone()["name"])

    monkeypatch.setattr(server, "main", server_main)
    assert cli.main(["--dsn", committing_dsn, "serve"]) == 0
    assert seen == [committing_dsn.removeprefix("dbname=")]


def test_a_topic_linked_to_an_action_holds_its_calls_until_unlinked(run, committing_dsn):
    import psycopg

    name, action = f"delegation-{uuid4().hex[:8]}", f"act{uuid4().hex[:8]}"
    run("topic", "--add", name, "--trigger", "Before handing work to a subagent")
    memory_id = remembered_id(run("remember", RULE, "--topic", name)[1])
    assert run("guard", action, "--json")[:2] == (0, "[]\n")

    assert run("topic", "--edit", name, "--action", action)[0] == 0
    code, out, _ = run("guard", action, "--json")
    assert code == 2
    assert json.loads(out) == [{"memory_id": memory_id, "content": RULE, "topic": name}]
    with psycopg.connect(committing_dsn) as conn:
        served = conn.execute(
            "SELECT e.detail ->> 'count', e.detail ->> 'topic_id' = t.topic_id::text "
            "FROM event_log e JOIN topic t ON t.name = %s "
            "WHERE e.event_type = 'guard_served' AND e.detail ->> 'action' = %s",
            (name, action),
        ).fetchall()
    assert served == [("1", True)]

    code, _, err = run("topic", "--add", "x", "--trigger", "t", "--action", action)
    assert code == 1 and "--edit" in err
    assert run("topic", "--edit", name, "--no-action")[0] == 0
    assert run("guard", action) == (0, "", "")


def test_a_candidate_is_admitted_only_at_the_version_that_was_reviewed(run, committing_dsn):
    code, out, _ = pain(run, EVIDENCED)
    assert code == 0 and "nomination  created" in out
    ledger_short = out.splitlines()[0].split()[1]
    code, out, _ = run("ledger", "--limit", "5")
    assert code == 0 and f"    prevention  {EVIDENCED}" in out

    code, out, _ = run("review", "--list")
    assert code == 0 and "evidence incident" in out
    nomination_id, reviewed = pending(out, EVIDENCED)

    code, _, error = run("review", "--admit", nomination_id)
    assert code == 1 and "--version" in error
    with db.transaction(committing_dsn) as cur:
        revised = f"apply every migration on the standby before it reaches the primary {uuid4()}"
        changed = nominations.revise(cur, UUID(nomination_id), content=revised, actor="user")
    assert changed["version"] > reviewed
    code, _, error = run("review", "--admit", nomination_id, "--version", str(reviewed))
    assert code == 1 and "nomination version changed" in error

    code, out, _ = run("review", "--admit", nomination_id[:8], "--version", str(changed["version"]))
    assert code == 0 and out.startswith("admitted")
    memory_id = out.split()[1]
    assert nomination_id not in run("review", "--list")[1]

    code, out, _ = run("show", memory_id)
    assert code == 0 and revised in out
    assert f"prevention  {EVIDENCED}" in out and ledger_short in out and "tokens" in out

    code, out, _ = run("show", ledger_short)
    assert code == 0 and f"prevention  {EVIDENCED}" in out
    assert "cited by" in out and memory_id[:8] in out


def test_a_memory_change_can_be_read_applied_and_replayed_from_the_cli(run, committing_dsn):
    with db.transaction(committing_dsn) as cur:
        memory = remember(cur, "the signer publishes the export manifest")
        proposal = propose_change(
            cur,
            memory,
            "retire",
            retirement_kind="out_of_scope",
            retire_reason="this export path has been removed",
        )

    _, listed, _ = run("review", "--changes", "--list")
    assert "the signer publishes the export manifest" in listed
    assert "this export path has been removed" in listed
    request_id = str(uuid4())
    command = ("review", "--apply-change", str(proposal["change_id"]), "--version", "1")
    first, replay = (run(*command, "--request-id", request_id) for _ in range(2))
    assert first == replay and first[0] == 0 and request_id in first[1]
    with db.transaction(committing_dsn) as cur:
        assert memories.get_memory(cur, memory["memory_id"])["status"] == "retired"


def test_a_candidate_put_off_is_listed_only_under_all_and_declined_only_with_a_reason(
    run, committing_dsn
):
    pain(run, DEFERRED)
    nomination_id, _ = pending(run("review", "--list")[1], DEFERRED)

    with db.transaction(committing_dsn) as cur:
        nominations.defer(cur, UUID(nomination_id), actor="user", reason="its owner is away")
    assert nomination_id not in run("review", "--list")[1]
    _, out, _ = run("review", "--list", "--all")
    assert nomination_id in out and "deferred: its owner is away" in out

    # A deferred candidate remains addressable by id.
    code, out, _ = run("review", "--decline", nomination_id, "--reason", "answered elsewhere")
    assert code == 0 and out.startswith("declined")
    assert nomination_id not in run("review", "--list", "--all")[1]


def test_a_dated_condition_is_written_by_the_command_line_and_nowhere_else(run):
    code, out, _ = run("remember", "the build host is down until Thursday", "--until", "3d")
    assert code == 0 and out.startswith("temporary")

    code, out, _ = run("bootstrap")
    assert code == 0
    assert "the build host is down until Thursday" in out

    code, _, err = run("remember", "staging is down", "--until", "2d", "--topic", "missing")
    assert code == 1 and "--topic" in err

    # Its own share of the opening now, apart from the memory seats (v3 8).
    code, out, _ = run("status")
    assert code == 0 and out.splitlines()[0] == "schema  up to date"
    line = next(row for row in out.splitlines() if row.startswith("temporary"))
    assert line.endswith(f"/{config.temporary_capacity()}")


def test_the_work_that_is_current_is_printed_under_its_date(run, committing_dsn):
    with db.transaction(committing_dsn) as cur:
        projects.create_project(cur, name="the command line", actor="user")
        task = tasks.task_create(
            cur,
            project="the command line",
            name=CURRENT_WORK,
            goal="print the active states with their headings",
            actor="agent",
        )

    code, out, _ = run("bootstrap")
    assert code == 0
    assert "active task cards" in out
    assert str(task["task"]["task_id"]) in out
    assert f"State as of {task['as_of'].isoformat()}" in out
    assert "print the active states with their headings" in out
    assert "call task_get" in out

    tokens_line = next(row for row in out.splitlines() if row.startswith("tokens"))
    assert f"/{config.total_capacity()}" in tokens_line
    assert "memory=" in tokens_line and "cards=" in tokens_line and "temporary=" in tokens_line

    _, out, _ = run("status")
    card_line = next(row for row in out.splitlines() if row.startswith("cards"))
    assert "active=" in card_line and "worst=" in card_line
    assert card_line.endswith(f"/{config.project_capacity()}")


def test_the_short_id_that_is_printed_is_the_one_that_can_be_typed_back(run):
    _, out, _ = run("remember", NAMED)
    memory_id = remembered_id(out)
    short = memory_id[:8]

    code, out, _ = run("show", short)
    assert code == 0
    assert memory_id in out
    assert NAMED in out

    code, out, _ = run("deliver", short, "always")
    assert code == 0 and out.startswith("delivery")

    code, _, err = run("show", "abc")
    assert code == 1 and "too short" in err

    code, out, err = run("retire", "not-a-uuid", "--kind", "invalidated", "--reason", "whatever")
    assert code == 1 and out == "" and "not an id" in err

    code, _, err = run("retire", NOWHERE, "--kind", "invalidated", "--reason", "no such row")
    assert code == 1 and "no memory begins with" in err

    code, _, err = run("show", NOWHERE)
    assert code == 1 and "no memory, nomination" in err


def test_a_prefix_two_rows_answer_to_is_refused_with_both_of_them(run, committing_dsn):
    import psycopg

    with psycopg.connect(committing_dsn, autocommit=True) as conn:
        ledger_id = conn.execute(
            "INSERT INTO ledger (kind, what, prevention, created_by) "
            "VALUES ('explicit', 'seeded for the collision', %s, 'test') RETURNING ledger_id",
            (TWIN,),
        ).fetchone()[0]
        for memory_id in (TWIN_A, TWIN_B):
            conn.execute(
                "INSERT INTO memory "
                "(memory_id, content, delivery, evidence, created_by) "
                "VALUES (%s, %s, 'always', %s, 'test')",
                (memory_id, TWIN, [ledger_id]),
            )

    code, _, err = run("retire", "abcd", "--kind", "invalidated", "--reason", "whichever it is")
    assert code == 1
    assert "names more than one memory" in err
    assert "abcd0001" in err and "abcd0002" in err

    code, _, err = run("show", "abcd")
    assert code == 1
    assert "names more than one row" in err
    assert err.count("memory") == 2

    code, out, _ = run("show", TWIN_A)
    assert code == 0 and TWIN_A in out


def test_the_memories_listing_holds_the_active_set_and_can_be_narrowed(run):
    _, out, _ = run("remember", LISTED)
    memory_id = remembered_id(out)

    run("scope", "--add", "listing scope", "--about", "a scope to list against")
    run("remember", SCOPED, "--scope", "listing scope")

    code, out, _ = run("memories")
    assert code == 0
    assert LISTED in out and SCOPED in out
    assert memory_id[:8] in out

    code, out, _ = run("memories", "--scope", "listing scope")
    assert code == 0
    assert SCOPED in out and LISTED not in out

    run("retire", memory_id[:8], "--kind", "invalidated", "--reason", "it grew a timezone")

    code, out, _ = run("memories")
    assert code == 0 and LISTED not in out

    code, out, _ = run("memories", "--retired")
    assert code == 0
    assert LISTED in out
    assert "it grew a timezone" in out


#: Japanese without spaces exercises cell-width wrapping.
TRACED = (
    "混合溶媒中の凝集判定は動径分布関数の第一ピークだけでは決められず、"
    "配位数の積分範囲を第一極小で切ったうえで両成分の温度依存を並べて初めて向きが読める。"
    "積分範囲を固定値で切った比較は範囲の選び方そのものを結論に持ち込む。"
)


def test_a_trace_is_shown_dated_named_and_wrapped_on_a_terminal(run, committing_dsn, monkeypatch):
    monkeypatch.setattr(cli, "_width", lambda: 60)
    with db.transaction(committing_dsn) as cur:
        left = trace_domain.put_trace(cur, content=TRACED, actor="agent")
    code, out, _ = run("trace")
    assert code == 0

    lines = out.splitlines()
    where = next(i for i, line in enumerate(lines) if line.startswith(str(left["trace_id"])[:8]))
    assert "  until " in lines[where]
    body = []
    for line in lines[where + 1 :]:
        if not line or not line.startswith("  "):
            break
        body.append(line)
    assert len(body) >= 3

    code, out, _ = run("trace", TRACED[:30])
    assert code == 0
    where = next(i for i, line in enumerate(out.splitlines()) if "match 0." in line)
    assert out.splitlines()[where].startswith(str(left["trace_id"])[:8])


#: Mixed Japanese and Latin text verifies wrapped spaces are preserved.
MIXED = (
    "実装完了と報告したが盤の声の立ち絵が一枚も無く、この検査は器の側で通っていた。"
    "verify_board も verify_story も立ち絵を測っていなかったのが原因で、"
    "gmx_rdf の -norm と InterRDF の norm 引数の食い違いと同じ形の見落としである。"
)


def test_wrapping_keeps_the_spaces_that_are_not_the_seam_itself(run):
    for width in range(40, 89):
        lines = cli._flow(MIXED, width=width).splitlines()
        # Rows must join without losing or duplicating a space.
        pos = 0
        for line in lines:
            chunk = line[2:]
            assert MIXED.startswith(chunk, pos), f"width {width}: {chunk!r} does not follow"
            pos += len(chunk)
            if pos < len(MIXED) and MIXED[pos] == " ":
                pos += 1
        assert pos == len(MIXED), f"width {width}: ends {len(MIXED) - pos} character(s) short"


def test_the_route_listing_keeps_the_tail_that_tells_two_routes_apart(run):
    run("scope", "--add", "the listing", "--about", "routes printed side by side")
    deep = "/listing/investigation/results/first-pass"
    wide = "/listing/調査/結果/一回目"
    run("route", "--add", deep, "--scope", "the listing")
    run("route", "--add", wide, "--scope", "the listing")

    _, out, _ = run("route")

    assert deep in out
    assert wide in out
    rows = [line for line in out.splitlines() if line.endswith("  the listing")]
    assert len(rows) == 2
    assert len({cli._cells(row) for row in rows}) == 1, out


#: Unique text prevents cross-test trigram matches.
REVIVED = "vacuum the audit table on the replica, and never while an export is running"
DELIVERED = "hold a lock across the whole rename, or a reader sees half of it applied"


def test_a_retired_rule_written_back_is_answered_with_the_reason_it_was_retired(run):
    memory_id = remembered_id(run("remember", REVIVED)[1])
    reason = "the replica lost its audit table in the last schema change"
    run("retire", memory_id, "--kind", "invalidated", "--reason", reason)

    code, out, err = run("remember", REVIVED)
    assert code == 1 and out == ""
    assert reason in err and "--force" in err

    code, out, _ = pain(run, REVIVED)
    assert code == 0 and "nomination  created" in out
    _, out, _ = run("review", "--list")
    assert "retired conflict" in out and "invalidated" in out and reason in out
    _, out, _ = run("show", pending(out, REVIVED)[0][:8])
    assert "contradicts retired" in out and reason in out

    code, out, _ = run("remember", REVIVED, "--force")
    assert code == 0 and "overrode  1 retirement" in out
    assert out.splitlines()[-1].startswith("remembered  ")


def test_a_pain_on_a_rule_already_delivered_names_the_delivery_and_is_counted(run):
    _, out, _ = run("remember", DELIVERED)
    memory_id = remembered_id(out)

    code, out, _ = pain(run, DELIVERED)
    assert code == 0
    assert "already active" in out
    assert f"active {memory_id[:8]} [always]" in out

    _, out, _ = run("status")
    suspected = next(line for line in out.splitlines() if line.startswith("delivery"))
    assert int(suspected.split()[1].split("=")[1]) >= 1
    assert "pushed=" in suspected


#: Every task test opens a project of its own.
PARSER_PROJECT = "the parser rewrite"
PARSER_TASK = "carry the byte offsets through the tokeniser"
PARSER_GOAL = "keep every diagnostic pointing at the character the reader typed"

DUPLICATE_PROJECT = "the vendored loader"
DUPLICATE_TASK = "replace the yaml loader we vendored two years ago"

HISTORY_PROJECT = "the exporter"
HISTORY_TASK = "give the exporter a manifest of what it wrote"
LOCATOR = "9d12f5a"
ATTEMPT = "counted the rows from the cursor instead of the file"
DECISION = "write the manifest last, so a half-run leaves none"
CHANGED = "the manifest is written and the count comes from it"

DORMANT_PROJECT = "the queue paperwork"
DORMANT_TASK = "collect the walltime figures for the allocation report"


def created_task(out: str) -> str:
    assert out.startswith("task  "), out
    return out.split()[1]


def test_a_task_is_created_read_back_ended_and_reopened_through_the_command_line(run):
    code, out, _ = run("project", "create", PARSER_PROJECT)
    assert code == 0 and out.startswith("created")

    code, out, _ = run(
        "task", "create", PARSER_TASK, "--project", PARSER_PROJECT, "--goal", PARSER_GOAL
    )
    assert code == 0
    short = created_task(out)

    code, out, _ = run("task", "list", "--project", PARSER_PROJECT)
    assert code == 0
    assert out.startswith("active tasks")
    assert short in out and PARSER_TASK in out
    # The date travels in the line itself, not in the caller's hands (v3 3.1).
    assert "State as of " in out

    code, out, _ = run("task", "show", short)
    assert code == 0
    assert PARSER_TASK in out and PARSER_GOAL in out
    assert "State as of " in out
    assert "open  active" in out

    code, out, _ = run("project", "list")
    assert code == 0
    assert PARSER_PROJECT in out

    code, out, _ = run("project", "show", PARSER_PROJECT)
    assert code == 0
    assert "active=1" in out
    assert short in out

    code, out, err = run("task", "close", short)
    assert code == 1 and out == "" and "--outcome" in err
    code, out, _ = run("task", "close", short, "--outcome", "completed", "--reason", "printed")
    assert code == 0 and out.startswith("closed") and "completed" in out
    assert run("task", "list", "--project", PARSER_PROJECT)[1].strip() == "no active tasks"
    _, out, _ = run("task", "list", "--closed", "--project", PARSER_PROJECT)
    assert short in out and "Final state as of " in out

    code, out, _ = run("task", "reopen", short)
    assert code == 0 and out.startswith("reopened")
    assert short in run("task", "list", "--project", PARSER_PROJECT)[1]
    code, out, _ = run("task", "touch", short)
    assert code == 0 and out.startswith("touched")


def test_a_task_that_reads_like_one_already_open_is_refused_with_the_candidates(run):
    run("project", "create", DUPLICATE_PROJECT)
    _, out, _ = run("task", "create", DUPLICATE_TASK, "--project", DUPLICATE_PROJECT)
    first = created_task(out)

    code, out, err = run("task", "create", DUPLICATE_TASK, "--project", DUPLICATE_PROJECT)
    assert code == 1
    assert out == ""
    assert first in err
    assert DUPLICATE_TASK in err
    assert "--force" in err

    code, out, _ = run("task", "create", DUPLICATE_TASK, "--project", DUPLICATE_PROJECT, "--force")
    assert code == 0
    assert created_task(out) != first


def test_the_history_and_the_artifact_locators_are_visible_on_one_task(run, committing_dsn):
    run("project", "create", HISTORY_PROJECT)
    _, out, _ = run("task", "create", HISTORY_TASK, "--project", HISTORY_PROJECT)
    short = created_task(out)

    with db.transaction(committing_dsn) as cur:
        task_id = cli._task_ref(cur, short)
        state = tasks.task_get(cur, task_id)["state"]
        task_history.checkpoint(
            cur,
            task_id,
            actor="agent",
            what_changed=CHANGED,
            expect_updated_at=state["updated_at"],
            status_text=CHANGED,
            attempts=[{"attempt": ATTEMPT, "result": "the count came out short"}],
            decisions=[{"decision": DECISION, "reason": "a half-run must not look whole"}],
            artifacts=[{"kind": "git_commit", "locator": LOCATOR, "label": "the manifest"}],
        )

    code, out, _ = run("task", "show", short)
    assert code == 0
    assert ATTEMPT in out and "the count came out short" in out
    assert DECISION in out and "a half-run must not look whole" in out
    assert CHANGED in out
    assert f"evidence  git_commit  {LOCATOR}" in out
    assert f"git_commit  {LOCATOR}  the manifest" in out


def test_a_task_whose_lease_has_run_out_is_listed_only_when_it_is_asked_for(run, committing_dsn):
    run("project", "create", DORMANT_PROJECT)
    _, out, _ = run("task", "create", DORMANT_TASK, "--project", DORMANT_PROJECT)
    short = created_task(out)

    with db.transaction(committing_dsn) as cur:
        cur.execute(
            "UPDATE task SET active_until = now() - interval '1 day' WHERE task_id::text LIKE %s",
            (f"{short}%",),
        )

    _, out, _ = run("task", "list", "--project", DORMANT_PROJECT)
    assert out.strip() == "no active tasks"

    code, out, _ = run("task", "list", "--dormant", "--project", DORMANT_PROJECT)
    assert code == 0
    assert short in out


# a checkout ahead of its database (13.2)
@pytest.mark.parametrize("command", [["review", "--changes", "--list"], ["status"]])
def test_a_store_behind_the_code_says_so_instead_of_raising(capsys, old_store, command):
    behind = old_store("0010_topics.sql")

    code = cli.main(["--dsn", behind, *command])
    captured = capsys.readouterr()
    assert code == 1 and "behind the code" in captured.err
    assert "0010_topics.sql" in captured.err
