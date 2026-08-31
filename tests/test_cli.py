"""The human command line (specification 8.2).

These run against the committing database, because the CLI opens its own
connection and commits: there is no transaction for the test to roll back
around it. Nothing here drives a terminal — the interactive review sitting is
out of scope, and every path it uses is reachable through a flag.
"""

from __future__ import annotations

import json

import pytest

from mashu import cli, db, nominations
from mashu import traces as trace_domain

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
DEFERRED = "name the branch after the ticket, so a stale checkout tells on itself"

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


def test_a_candidate_put_off_is_out_of_the_listing_until_all_asks_for_it(run, committing_dsn):
    """Deferral is written by the sitting, so the listing is what is checked here."""
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

    # Naming it directly still works: putting something off is not a decision,
    # so it does not put the candidate out of reach.
    code, out, _ = run("review", "--decline", nomination_id, "--reason", "answered elsewhere")
    assert code == 0 and out.startswith("declined")


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


#: Japanese on purpose, and without a single space: the wrapping has to break
#: it by cell width, because textwrap would have kept it as one line.
TRACED = (
    "混合溶媒中の凝集判定は動径分布関数の第一ピークだけでは決められず、"
    "配位数の積分範囲を第一極小で切ったうえで両成分の温度依存を並べて初めて向きが読める。"
    "積分範囲を固定値で切った比較は範囲の選び方そのものを結論に持ち込む。"
)


def test_a_trace_is_shown_dated_named_and_wrapped_instead_of_as_one_long_line(run, committing_dsn):
    """A trace is a long dated observation, and one unwrapped line buries it."""
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


#: Japanese with spaced Latin identifiers threaded through it, the mixture the
#: real traces hold. A space that is not the seam itself has to survive the
#: wrap, or two identifiers fuse into one that exists nowhere.
MIXED = (
    "実装完了と報告したが盤の声の立ち絵が一枚も無く、この検査は器の側で通っていた。"
    "verify_board も verify_story も立ち絵を測っていなかったのが原因で、"
    "gmx_rdf の -norm と InterRDF の norm 引数の食い違いと同じ形の見落としである。"
)


def test_wrapping_keeps_the_spaces_that_are_not_the_seam_itself(run):
    """Words may move to the next row, but no two of them fuse at a seam.

    Swept across every width rather than one, because the fusion only happens
    when a space lands exactly on the overflow after an earlier cut, and a
    single width either hits that geometry or silently proves nothing.
    """
    for width in range(40, 89):
        lines = cli._flow(MIXED, width=width).splitlines()
        # Aligned against the original: each row must read on from where the
        # last one stopped, and only the seam itself may stand for a space.
        pos = 0
        for line in lines:
            chunk = line[2:]
            assert MIXED.startswith(chunk, pos), f"width {width}: {chunk!r} does not follow"
            pos += len(chunk)
            if pos < len(MIXED) and MIXED[pos] == " ":
                pos += 1
        assert pos == len(MIXED), f"width {width}: ends {len(MIXED) - pos} character(s) short"


def test_the_route_listing_keeps_the_tail_that_tells_two_routes_apart(run):
    """A route is identified by its end, and a Japanese one prints twice as wide.

    Cutting the listing at a fixed column printed every route under one tree
    as the same row, which is the one reading a person consults this table to
    settle. Padding by character count is the same mistake reached from the
    other side: the scope beside a Japanese path stepped out of its column.
    """
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


#: Kept apart from every other string in this file for the same reason as the
#: rest: these two tests turn on a trigram match, so a stray resemblance to
#: another test's rule would decide them for the wrong reason.
REVIVED = "vacuum the audit table on the replica, and never while an export is running"
DELIVERED = "hold a lock across the whole rename, or a reader sees half of it applied"


def test_writing_a_retired_rule_back_is_stopped_until_the_reason_has_been_read(run):
    """Section 5.3 lets a person overrule a retirement, knowingly.

    With nothing to type at, there is nothing to read at either, so the write
    is refused and the flag that says the reason was read elsewhere is named.
    """
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
    """The falsification criterion, on the screen a person opens (12)."""
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
    """Every screen a candidate is read from has to carry the collision.

    One that shows on the sitting and on `show` but not on the listing hides
    on whichever screen the reader happened to open.
    """
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
