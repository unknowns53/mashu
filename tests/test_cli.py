"""The human command line (specification 8.2).

These run against the committing database, because the CLI opens its own
connection and commits: there is no transaction for the test to roll back
around it. Nothing here drives a terminal — the interactive review sitting is
out of scope, and every path it uses is reachable through a flag.
"""

from __future__ import annotations

import json

import pytest

from mashu import cli

# Each test writes to a database the others also write to, so the strings are
# kept mutually dissimilar: a trigram match across tests would make one test's
# tombstone or pending candidate answer another test's report.
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

#: A hexadecimal string no printed id can begin with: the ninth character of a
#: UUID is always a dash, so twelve hexadecimal digits match nothing, whatever
#: the store happens to hold.
NOWHERE = "ffffffffffff"

#: Two ids that agree for four characters and part at the fifth. Real ids are
#: random, so a collision cannot be arranged by writing rows through the
#: command line; these are seeded directly to get one.
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


def test_a_pinned_rule_holds_the_act_and_lets_go_when_it_is_unpinned(run):
    """The gate's contract is the exit code, which is what the hook reads.

    Two means a rule was put in front of this act and the call should be made
    again having read it; zero means there was nothing to say.
    """
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


def test_a_dated_condition_is_written_by_the_command_line_and_nowhere_else(run):
    """Temporary context is pushed, so only a person writes it (7)."""
    code, out, _ = run("remember", "the build host is down until Thursday", "--until", "3d")
    assert code == 0 and out.startswith("temporary")

    code, out, _ = run("bootstrap")
    assert code == 0
    assert "the build host is down until Thursday" in out

    code, _, err = run("remember", "something", "--until", "30d")
    assert code == 1 and "14" in err


def test_a_pain_that_lands_on_retired_knowledge_is_answered_with_the_reason(run):
    """The tombstone is the answer, and no candidate is filed behind it."""
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
    """Everything here prints eight characters, so eight characters must work."""
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
    """An ambiguous reference is a question, and guessing an answer loses rows."""
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
    """The evidence is the point of the standard, so it has to stay legible."""
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
