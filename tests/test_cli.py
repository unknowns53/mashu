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
    assert "invalid UUID" in err
