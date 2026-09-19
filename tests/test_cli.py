
from __future__ import annotations

import json
import os

import pytest

from mashu import cli, config, db, nominations, projects, task_history, tasks
from mashu import traces as trace_domain
from mashu.migrate import migrate

ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")

# Keep test strings dissimilar to prevent cross-test trigram matches.
RULE = "keep the deployment order in the runbook rather than in anyone's head"
QUOTA = "the shared queue resets its quota at midnight, not at the hour"
WITHDRAWN_RULE = "compile the extension against the vendored headers, never the system ones"
CARRIED = "prefer the smaller of two equivalent schemas when both are already written"
NAMED = "hold the terminal wide open while a long import prints, or watch it wrap badly"
TWIN = "wipe the scratch directory between two runs of the packager, always"
EVIDENCED = "apply a migration on the standby first; the primary is not the rehearsal"
LISTED = "spell out the timezone in every scheduled job, even when it looks obvious"
SCOPED = "reach for the vendored toolchain in this checkout, not whatever is on PATH"
PREVENTED = "read the exporter's manifest before trusting the row count it prints"
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


def remembered_id(out: str) -> str:
    assert out.startswith("remembered  "), out
    return out.split()[1]


def pending_id(out: str, content: str) -> str:
    """The nomination id whose proposed body is this, from `review --list`."""
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == content and index and not lines[index - 1].startswith(" "):
            return lines[index - 1].split()[0]
    raise AssertionError(f"no pending nomination proposing {content!r} in:\n{out}")


HELP_PATHS = (
    ("status",),
    ("bootstrap",),
    ("remember",),
    ("retire",),
    ("revise",),
    ("show",),
    ("memories",),
    ("pain",),
    ("ledger",),
    ("trace",),
    ("review",),
    ("deliver",),
    ("guard",),
    ("scope",),
    ("route",),
    ("project",),
    ("project", "list"),
    ("project", "create"),
    ("project", "show"),
    ("task",),
    ("task", "list"),
    ("task", "show"),
    ("task", "create"),
    ("task", "touch"),
    ("task", "close"),
    ("task", "reopen"),
    ("serve",),
    ("admin",),
    ("admin", "migrate"),
)


def test_top_level_help_explains_the_cli_to_people_and_agents(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])

    out, err = capsys.readouterr()
    prose = " ".join(out.split())
    assert stopped.value.code == 0 and err == ""
    assert "Start here:" in out
    assert "mashu COMMAND --help" in out
    assert "Any unique prefix" in prose
    assert "human-facing CLI" in prose and "Mashu MCP tools" in prose
    assert "MASHU_DATABASE_URL" in out and "MASHU_AGENT" in out


@pytest.mark.parametrize("path", HELP_PATHS, ids=lambda path: "-".join(path))
def test_every_command_help_has_a_description_and_an_example(path, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([*path, "--help"])

    out, err = capsys.readouterr()
    assert stopped.value.code == 0 and err == ""
    assert f"usage: mashu {' '.join(path)}" in out
    assert "examples:\n  mashu " in out


@pytest.mark.parametrize(
    ("path", "needles"),
    (
        (("remember",), ("temporary condition", "cannot be combined")),
        (("review",), ("interactive review UI", "User decisions")),
        (("deliver",), ("Scope delivery requires --scope", "guard delivery requires --action")),
        (("guard",), ("exits 2", "empty gate exits 0")),
        (("scope",), ("both --add and --about", "User-only")),
        (("route",), ("--add requires --scope", "--ignore")),
        (("task", "close"), ("explicit outcome", "User's decision")),
        (("serve",), ("MCP stdio server", "MASHU_AGENT")),
    ),
)
def test_command_help_states_runtime_constraints(path, needles, capsys):
    with pytest.raises(SystemExit):
        cli.main([*path, "--help"])

    out, _ = capsys.readouterr()
    prose = " ".join(out.split())
    for needle in needles:
        assert needle in prose


def test_a_pinned_rule_holds_the_act_and_lets_go_when_it_is_unpinned(run):
    _, out, _ = run("remember", RULE)
    memory_id = remembered_id(out)

    code, out, _ = run("guard", "Bash", "--pin", memory_id)
    assert code == 0 and out.startswith("pinned")

    code, out, _ = run("guard", "Bash")
    assert code == 2
    assert out.strip() == RULE

    code, out, _ = run("guard", "Bash", "--json")
    assert code == 2
    assert json.loads(out) == [{"memory_id": memory_id, "content": RULE}]

    code, _, _ = run("guard", "Bash", "--unpin", memory_id)
    assert code == 0

    code, out, _ = run("guard", "Bash")
    assert code == 0 and out == ""


def test_serving_a_gate_is_recorded_but_an_empty_one_is_not(run, committing_dsn):
    import psycopg

    run("guard", "NothingIsPinnedHere")
    with psycopg.connect(committing_dsn) as conn:
        rows = conn.execute(
            "SELECT count(*) AS n FROM event_log WHERE event_type = 'guard_served' "
            "AND detail ->> 'action' = 'NothingIsPinnedHere'"
        ).fetchone()
    assert rows[0] == 0


def test_a_candidate_can_be_read_then_admitted(run):
    code, out, _ = run(
        "pain", "--kind", "incident", "--what", "shipped it twice", "--prevention", QUOTA
    )
    assert code == 0
    assert "nomination  created" in out

    code, out, _ = run("review", "--list")
    assert code == 0
    nomination_id = pending_id(out, QUOTA)
    assert "evidence incident" in out

    code, out, _ = run("review", "--admit", nomination_id)
    assert code == 0 and out.startswith("admitted")

    code, out, _ = run("review", "--list")
    assert nomination_id not in out


def test_a_candidate_can_be_turned_down_and_needs_a_reason_to_be(run):
    run("pain", "--kind", "incident", "--what", "built it wrong", "--prevention", CARRIED)
    _, out, _ = run("review", "--list")
    nomination_id = pending_id(out, CARRIED)

    code, _, err = run("review", "--decline", nomination_id)
    assert code == 1 and "requires --reason" in err

    code, out, _ = run("review", "--decline", nomination_id, "--reason", "said in passing")
    assert code == 0 and out.startswith("declined")

    _, out, _ = run("review", "--list")
    assert nomination_id not in out


def test_a_candidate_put_off_is_out_of_the_listing_until_all_asks_for_it(run, committing_dsn):
    run("pain", "--kind", "incident", "--what", "guessed at it", "--prevention", DEFERRED)
    _, out, _ = run("review", "--list")
    nomination_id = pending_id(out, DEFERRED)

    with db.transaction(committing_dsn) as cur:
        nominations.defer(
            cur,
            cli._resolve(
                cur, nomination_id, table="nomination", id_col="nomination_id", label="candidate"
            ),
            actor="user",
            reason="the owner of that runbook is away",
        )

    _, out, _ = run("review", "--list")
    assert nomination_id not in out

    _, out, _ = run("review", "--list", "--all")
    assert nomination_id in out
    assert "deferred: the owner of that runbook is away" in out

    # A deferred candidate remains addressable by id.
    code, out, _ = run("review", "--decline", nomination_id, "--reason", "answered elsewhere")
    assert code == 0 and out.startswith("declined")


def test_a_dated_condition_is_written_by_the_command_line_and_nowhere_else(run):
    code, out, _ = run("remember", "the build host is down until Thursday", "--until", "3d")
    assert code == 0 and out.startswith("temporary")

    code, out, _ = run("bootstrap")
    assert code == 0
    assert "the build host is down until Thursday" in out

    code, _, err = run("remember", "something", "--until", "30d")
    assert code == 1 and "14" in err

    # Its own share of the opening now, apart from the memory seats (v3 8).
    _, out, _ = run("status")
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
    assert "project state" in out
    assert str(task["task"]["task_id"])[:8] in out
    assert f"State as of {task['as_of'].isoformat()}" in out
    assert "print the active states with their headings" in out

    tokens_line = next(row for row in out.splitlines() if row.startswith("tokens"))
    assert f"/{config.total_capacity()}" in tokens_line
    assert "memory=" in tokens_line and "state=" in tokens_line and "temporary=" in tokens_line

    _, out, _ = run("status")
    state_line = next(row for row in out.splitlines() if row.startswith("state"))
    assert "active=" in state_line and "worst=" in state_line
    assert state_line.endswith(f"/{config.project_capacity()}")


def test_status_says_where_the_schema_stands_before_it_reports_any_count(run):
    code, out, _ = run("status")
    assert code == 0
    assert out.splitlines()[0] == "schema  up to date"


def test_a_pain_that_lands_on_retired_knowledge_is_answered_with_the_reason(run):
    _, out, _ = run("remember", WITHDRAWN_RULE)
    memory_id = remembered_id(out)
    run("retire", memory_id, "--reason", "the vendored headers were dropped upstream")

    code, out, _ = run(
        "pain",
        "--kind",
        "incident",
        "--what",
        "built against the wrong headers",
        "--prevention",
        WITHDRAWN_RULE,
    )
    assert code == 0
    assert "nomination  withheld" in out
    assert "the vendored headers were dropped upstream" in out

    _, out, _ = run("review", "--list")
    assert WITHDRAWN_RULE not in out


def test_a_refusal_leaves_by_the_error_channel_with_a_failing_code(run):
    code, out, err = run("retire", "not-a-uuid", "--reason", "whatever")
    assert code == 1
    assert out == ""
    assert "not an id" in err


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

    code, _, err = run("retire", NOWHERE, "--reason", "there is no such row")
    assert code == 1 and "no memory begins with" in err

    code, _, err = run("show", NOWHERE)
    assert code == 1 and "nothing here answers to" in err


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
                "(memory_id, content, delivery, guard_action, evidence, created_by) "
                "VALUES (%s, %s, 'guard', 'TwinAct', %s, 'test')",
                (memory_id, TWIN, [ledger_id]),
            )

    code, _, err = run("retire", "abcd", "--reason", "whichever it is")
    assert code == 1
    assert "names more than one memory" in err
    assert "abcd0001" in err and "abcd0002" in err

    code, _, err = run("show", "abcd")
    assert code == 1
    assert "names more than one row" in err
    assert err.count("memory") == 2

    code, out, _ = run("show", TWIN_A)
    assert code == 0 and TWIN_A in out


def test_what_a_memory_rests_on_can_still_be_read_after_it_is_admitted(run):
    code, out, _ = run(
        "pain",
        "--kind",
        "incident",
        "--what",
        "ran the migration on the primary first",
        "--prevention",
        EVIDENCED,
    )
    assert code == 0
    ledger_short = out.splitlines()[0].split()[1]

    _, out, _ = run("review", "--list")
    nomination_id = pending_id(out, EVIDENCED)

    code, out, _ = run("show", nomination_id[:8])
    assert code == 0
    assert "pending" in out
    assert EVIDENCED in out

    code, out, _ = run("review", "--admit", nomination_id[:8])
    assert code == 0
    memory_id = out.split()[1]

    code, out, _ = run("show", memory_id)
    assert code == 0
    assert f"prevention  {EVIDENCED}" in out
    assert ledger_short in out
    assert "tokens" in out

    code, out, _ = run("show", ledger_short)
    assert code == 0
    assert f"prevention  {EVIDENCED}" in out
    assert "cited by" in out
    assert memory_id[:8] in out


def test_the_memories_listing_holds_the_active_set_and_can_be_narrowed(run):
    _, out, _ = run("remember", LISTED)
    memory_id = remembered_id(out)

    run("scope", "--add", "listing scope", "--about", "a scope to list against")
    run("remember", SCOPED, "--scope", "listing scope")

    code, out, _ = run("memories")
    assert code == 0
    assert out.splitlines()[0].startswith("id")
    assert LISTED in out and SCOPED in out
    assert memory_id[:8] in out

    code, out, _ = run("memories", "--scope", "listing scope")
    assert code == 0
    assert SCOPED in out and LISTED not in out

    run("retire", memory_id[:8], "--reason", "the scheduler grew a timezone column")

    code, out, _ = run("memories")
    assert code == 0 and LISTED not in out

    code, out, _ = run("memories", "--retired")
    assert code == 0
    assert LISTED in out
    assert "the scheduler grew a timezone column" in out


def test_the_ledger_listing_shows_the_sentence_the_matching_runs_on(run):
    run(
        "pain",
        "--kind",
        "friction",
        "--what",
        "counted the exported rows by hand again",
        "--prevention",
        PREVENTED,
    )
    code, out, _ = run("ledger", "--limit", "5")
    assert code == 0
    assert f"    prevention  {PREVENTED}" in out


#: Japanese without spaces exercises cell-width wrapping.
TRACED = (
    "混合溶媒中の凝集判定は動径分布関数の第一ピークだけでは決められず、"
    "配位数の積分範囲を第一極小で切ったうえで両成分の温度依存を並べて初めて向きが読める。"
    "積分範囲を固定値で切った比較は範囲の選び方そのものを結論に持ち込む。"
)


def test_a_trace_is_shown_dated_named_and_wrapped_instead_of_as_one_long_line(run, committing_dsn):
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
    # Wrapped into several indented rows, none of it lost at the seams.
    assert len(body) >= 3
    assert "".join(line.strip() for line in body) == TRACED

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


def test_writing_a_retired_rule_back_is_stopped_until_the_reason_has_been_read(run):
    _, out, _ = run("remember", REVIVED)
    memory_id = remembered_id(out)
    reason = "the replica lost its audit table in the last schema change"
    run("retire", memory_id, "--reason", reason)

    code, out, err = run("remember", REVIVED)
    assert code == 1
    assert out == ""
    assert reason in err
    assert "--force" in err

    code, out, _ = run("remember", REVIVED, "--force")
    assert code == 0
    assert "overrode  1 retirement" in out
    assert out.splitlines()[-1].startswith("remembered  ")


def test_a_pain_on_a_rule_already_delivered_names_the_delivery_and_is_counted(run):
    _, out, _ = run("remember", DELIVERED)
    memory_id = remembered_id(out)

    code, out, _ = run(
        "pain",
        "--kind",
        "incident",
        "--what",
        "a reader saw the rename half applied",
        "--prevention",
        DELIVERED,
    )
    assert code == 0
    assert "already active" in out
    assert f"active {memory_id[:8]} [always]" in out

    _, out, _ = run("review", "--list")
    assert DELIVERED not in out

    _, out, _ = run("status")
    suspected = next(line for line in out.splitlines() if line.startswith("delivery"))
    assert int(suspected.split("=")[1]) >= 1


#: Kept apart from the rest for the same trigram reason as the two above.
CARRIED_BACK = "give the batch job its own credentials, never the operator's session token"


def test_a_carried_instruction_that_repeats_a_retirement_says_so_in_the_listing(
    run, committing_dsn
):
    _, out, _ = run("remember", CARRIED_BACK)
    memory_id = remembered_id(out)
    reason = "the batch job runs unattended now and has no session to borrow"
    run("retire", memory_id, "--reason", reason)

    with db.transaction(committing_dsn) as cur:
        carried = nominations.nominate_user_explicit(cur, content=CARRIED_BACK, actor="agent")
    assert carried["tombstone_conflict"] is True

    _, out, _ = run("review", "--list")
    assert "contradicts retired" in out
    assert reason in out

    _, out, _ = run("show", str(carried["nomination"]["nomination_id"])[:8])
    assert "contradicts retired" in out
    assert reason in out


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

CLOSING_PROJECT = "the poster"
CLOSING_TASK = "redraw the phase diagram at the printer's resolution"

DORMANT_PROJECT = "the queue paperwork"
DORMANT_TASK = "collect the walltime figures for the allocation report"

AMBIGUOUS_PROJECT = "the collision bench"

#: Seed UUIDs sharing a four-character prefix to test ambiguity handling.
TWIN_TASK_A = "abcd0003-0000-4000-8000-00000000000c"
TWIN_TASK_B = "abcd0004-0000-4000-8000-00000000000d"


def created_task(out: str) -> str:
    assert out.startswith("task  "), out
    return out.split()[1]


def test_a_task_is_created_listed_and_read_back_through_the_command_line(run):
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
        artifact = task_history.artifact_link(
            cur, task_id, actor="agent", kind="git_commit", locator=LOCATOR, label="the manifest"
        )
        task_history.attempt_record(
            cur, task_id, actor="agent", attempt=ATTEMPT, result="the count came out short"
        )
        task_history.decision_record(
            cur, task_id, actor="agent", decision=DECISION, reason="a half-run must not look whole"
        )
        state = tasks.task_get(cur, task_id)["state"]
        task_history.checkpoint(
            cur,
            task_id,
            actor="agent",
            what_changed=CHANGED,
            expect_updated_at=state["updated_at"],
            status_text=CHANGED,
            evidence=[artifact["reference_id"]],
        )

    code, out, _ = run("task", "show", short)
    assert code == 0
    assert ATTEMPT in out and "the count came out short" in out
    assert DECISION in out and "a half-run must not look whole" in out
    assert CHANGED in out
    assert f"evidence  git_commit  {LOCATOR}" in out
    assert f"git_commit  {LOCATOR}  the manifest" in out


def test_ending_a_task_needs_an_outcome_and_can_be_taken_back(run):
    run("project", "create", CLOSING_PROJECT)
    _, out, _ = run("task", "create", CLOSING_TASK, "--project", CLOSING_PROJECT)
    short = created_task(out)

    code, out, err = run("task", "close", short)
    assert code == 1
    assert out == ""
    assert "--outcome" in err

    code, out, _ = run("task", "close", short, "--outcome", "completed", "--reason", "printed")
    assert code == 0 and out.startswith("closed") and "completed" in out

    _, out, _ = run("task", "list", "--project", CLOSING_PROJECT)
    assert out.strip() == "no active tasks"

    _, out, _ = run("task", "list", "--closed", "--project", CLOSING_PROJECT)
    assert short in out
    assert "Final state as of " in out

    code, out, _ = run("task", "reopen", short)
    assert code == 0 and out.startswith("reopened")

    _, out, _ = run("task", "list", "--project", CLOSING_PROJECT)
    assert short in out

    code, out, _ = run("task", "touch", short)
    assert code == 0 and out.startswith("touched")


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
    # Not the present tense: what it holds is the last thing anybody confirmed.
    assert "Last known state as of " in out

    _, out, _ = run("bootstrap")
    assert short not in out


def test_a_task_prefix_two_rows_answer_to_is_refused_with_both_of_them(run, committing_dsn):
    import psycopg

    with psycopg.connect(committing_dsn, autocommit=True) as conn:
        project_id = conn.execute(
            "INSERT INTO project (name) VALUES (%s) RETURNING project_id",
            (AMBIGUOUS_PROJECT,),
        ).fetchone()[0]
        for task_id in (TWIN_TASK_A, TWIN_TASK_B):
            conn.execute(
                "INSERT INTO task (task_id, project_id, name, created_by, active_until) "
                "VALUES (%s, %s, %s, 'test', now() + interval '14 days')",
                (task_id, project_id, f"seeded for the collision {task_id[:8]}"),
            )
            conn.execute(
                "INSERT INTO task_state (task_id, updated_by) VALUES (%s, 'test')", (task_id,)
            )

    code, _, err = run("task", "show", "abcd")
    assert code == 1
    assert "names more than one task" in err
    assert "abcd0003" in err and "abcd0004" in err

    code, _, err = run("task", "touch", "abc")
    assert code == 1 and "too short" in err

    code, _, err = run("task", "show", NOWHERE)
    assert code == 1 and "no task begins with" in err

    code, out, _ = run("task", "show", TWIN_TASK_A[:8])
    assert code == 0
    assert TWIN_TASK_A in out


# a checkout ahead of its database (13.2)
def test_a_store_behind_the_code_says_so_instead_of_raising(capsys, tmp_path):
    import pathlib
    import shutil

    import psycopg

    from mashu.migrate import migration_files

    name = "mashu_test_behind"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    partial = pathlib.Path(tmp_path / "migrations")
    partial.mkdir()
    for path in migration_files()[:-1]:
        shutil.copy(path, partial / path.name)
    migrate(f"dbname={name}", partial)

    code = cli.main(["--dsn", f"dbname={name}", "task", "list"])
    captured = capsys.readouterr()
    assert code == 1
    assert "behind the code" in captured.err
    assert migration_files()[-1].name in captured.err
