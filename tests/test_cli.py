"""The review command line (specifications 18, 18.1).

These tests commit rather than roll back, because the CLI opens its own
connection: that is the path being tested, and faking it would leave the part
that actually runs untested. Each test works in its own scope so the shared
database does not make them depend on each other.
"""

from __future__ import annotations

import json
import uuid

import pytest

from mashu import cli, proposals, store
from mashu.db import transaction
from mashu.models import MemoryType, ProposalOperation, SourceType


@pytest.fixture
def test_dsn(committing_dsn):
    """These tests commit, so they run on the database built for that."""
    return committing_dsn


@pytest.fixture
def run(test_dsn, capsys):
    def _run(*argv):
        code = cli.main(["--dsn", test_dsn, *argv])
        return code, capsys.readouterr().out

    return _run


@pytest.fixture
def committed_scope(test_dsn):
    with transaction(test_dsn) as cur:
        return store.create_scope(cur, name=f"cli scope {uuid.uuid4()}", actor="user")


def _propose(test_dsn, scope_id, title, content, type=MemoryType.OBSERVATION, session_id=None):
    with transaction(test_dsn) as cur:
        return proposals.propose(
            cur,
            actor="claude",
            operation=ProposalOperation.CREATE,
            payload={
                "scope_id": str(scope_id),
                "type": str(type),
                "title": title,
                "content": content,
                "source_type": str(SourceType.AGENT),
            },
            session_id=session_id,
            allow_similar=True,
            allow_duplicate=True,
        )["proposal"]


def test_the_queue_names_the_bundle_and_its_wait(run, test_dsn, committed_scope):
    with transaction(test_dsn) as cur:
        cur.execute("INSERT INTO agent_session (agent) VALUES ('claude') RETURNING session_id")
        session_id = cur.fetchone()["session_id"]

    _propose(
        test_dsn, committed_scope, "cli decision", "so we split it", MemoryType.DECISION, session_id
    )
    _propose(
        test_dsn,
        committed_scope,
        "cli observation",
        "the exit code was one",
        MemoryType.OBSERVATION,
        session_id,
    )

    code, out = run("queue")
    assert code == 0
    assert str(session_id)[:8] in out
    assert out.index("cli observation") < out.index("cli decision"), (
        "the bundle has to read grounds before conclusions"
    )


def test_show_prints_the_proposed_text(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "cli show", "the enclosure timed out again")
    code, out = run("show", str(proposal["proposal_id"])[:8])
    assert code == 0
    assert "the enclosure timed out again" in out


def test_approving_by_prefix_makes_it_current(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "cli approve", "the ramp is half a degree")
    code, out = run("approve", str(proposal["proposal_id"])[:8], "--reason", "checked")
    assert code == 0

    with transaction(test_dsn) as cur:
        entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] == proposal["applied_version"]


def test_rejecting_records_the_reason(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "cli reject", "an uncalibrated reading")
    code, _ = run("reject", str(proposal["proposal_id"])[:8], "--reason", "wrong thermocouple")
    assert code == 0

    with transaction(test_dsn) as cur:
        decided = proposals.get(cur, proposal["proposal_id"])
    assert decided["decision_reason"] == "wrong thermocouple"


def test_an_ambiguous_prefix_stops_rather_than_guessing(run, test_dsn, committed_scope):
    """Deciding the wrong proposal is not recoverable by re-running the command."""
    _propose(test_dsn, committed_scope, "cli ambiguous one", "some content")
    _propose(test_dsn, committed_scope, "cli ambiguous two", "other content")
    with pytest.raises(SystemExit) as caught:
        run("show", "")
    assert "use more characters" in str(caught.value)


def test_an_unknown_prefix_stops(run):
    with pytest.raises(SystemExit):
        run("show", "ffffffffff")


def test_search_prints_all_three_layers(run, test_dsn, committed_scope):
    with transaction(test_dsn) as cur:
        store.create_entity(
            cur,
            scope_id=committed_scope,
            type=MemoryType.FACT,
            title="cli search active",
            content="the searchable ramp is half a degree per minute",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=True,
        )
    code, out = run("search", "searchable ramp")
    assert code == 0
    assert "layer 1  active" in out
    assert "layer 2  unreviewed" in out
    assert "layer 3  retired" in out
    assert "the searchable ramp is half a degree per minute" in out


def test_the_queue_warns_about_candidates_no_review_can_reach(run, test_dsn, committed_scope):
    """A candidate written straight into the store is visible but unsettleable."""
    with transaction(test_dsn) as cur:
        store.create_entity(
            cur,
            scope_id=committed_scope,
            type=MemoryType.FACT,
            title="cli orphan",
            content="written without a proposal",
            source_type=SourceType.FILE,
            created_by="import",
            actor="import",
            adopt=False,
        )
    code, out = run("queue")
    assert code == 0
    assert "no pending proposal" in out
    assert "cli orphan" in out


def test_backfill_embeds_what_was_written_without_a_vector(run, test_dsn, committed_scope):
    with transaction(test_dsn) as cur:
        memory_id, version_id = store.create_entity(
            cur,
            scope_id=committed_scope,
            type=MemoryType.FACT,
            title="cli backfill",
            content="written before the model was wired in",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=True,
        )
        cur.execute(
            "UPDATE memory_entity SET title_embedding = NULL WHERE memory_id = %s", (memory_id,)
        )
        cur.execute(
            "UPDATE memory_version SET content_embedding = NULL WHERE version_id = %s",
            (version_id,),
        )

    code, out = run("backfill")
    assert code == 0

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT title_embedding IS NOT NULL AS ok FROM memory_entity WHERE memory_id = %s",
            (memory_id,),
        )
        assert cur.fetchone()["ok"]
        cur.execute(
            "SELECT content_embedding IS NOT NULL AS ok FROM memory_version WHERE version_id = %s",
            (version_id,),
        )
        assert cur.fetchone()["ok"]


def test_import_places_a_file_of_memories_into_a_scope(run, test_dsn, committed_scope, tmp_path):
    import json

    with transaction(test_dsn) as cur:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (committed_scope,))
        scope_name = cur.fetchone()["name"]

    path = tmp_path / "memories.json"
    path.write_text(
        json.dumps(
            [
                {
                    "type": "observation",
                    "title": "cli import subject",
                    "content": "the enclosure timed out on three separate drives",
                    "source_reference": "memory/ssd.md",
                },
                {"type": "fact", "title": "cli import no origin", "content": "unattributed"},
            ]
        ),
        encoding="utf-8",
    )

    code, out = run("import", str(path), "--scope", scope_name)
    assert code == 0
    assert "1 new entity proposal(s)" in out
    assert "cli import subject" in out
    assert "source_reference" in out, "the item without an origin has to be reported"


def test_an_import_run_is_one_bundle(run, test_dsn, committed_scope, tmp_path):
    """18.1 has the reviewer rebuild context once per bundle.

    Leaving a migration unattached puts every imported file into the single
    unnamed bundle, whose size then grows with the store rather than with the
    run, and the one thing the bundle was for is gone.
    """
    import json

    with transaction(test_dsn) as cur:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (committed_scope,))
        scope_name = cur.fetchone()["name"]

    path = tmp_path / "run.json"
    path.write_text(
        json.dumps(
            [
                {
                    "type": "observation",
                    "title": f"bundled import {i}",
                    "content": f"the {i}th reading of the enclosure timing out",
                    "source_reference": f"memory/run-{i}.md",
                }
                for i in range(3)
            ]
        ),
        encoding="utf-8",
    )

    code, out = run("import", str(path), "--scope", scope_name)
    assert code == 0
    assert "bundle " in out

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT DISTINCT session_id FROM proposal WHERE (payload ->> 'scope_id')::uuid = %s",
            (committed_scope,),
        )
        sessions = [row["session_id"] for row in cur.fetchall()]
    assert len(sessions) == 1
    assert sessions[0] is not None


def test_a_bundle_can_be_read_as_one_document(run, test_dsn, committed_scope):
    """18.1 makes the bundle the unit because rebuilding context is the cost.

    Showing sixty-three proposals one at a time puts that cost straight back,
    so the whole bundle prints as one reading, in the order the section asks
    for.
    """
    import uuid as _uuid

    with transaction(test_dsn) as cur:
        cur.execute("INSERT INTO agent_session (agent) VALUES ('claude') RETURNING session_id")
        session_id = cur.fetchone()["session_id"]

    _propose(
        test_dsn,
        committed_scope,
        f"the conclusion {_uuid.uuid4()}",
        "so we kept the runner",
        type=MemoryType.DECISION,
        session_id=session_id,
    )
    _propose(
        test_dsn,
        committed_scope,
        f"the grounds {_uuid.uuid4()}",
        "the exit code was 1",
        type=MemoryType.OBSERVATION,
        session_id=session_id,
    )

    code, out = run("show", "--bundle", str(session_id)[:8])
    assert code == 0
    assert "2 proposal(s)" in out
    assert "the exit code was 1" in out
    assert "so we kept the runner" in out
    assert out.index("the exit code was 1") < out.index("so we kept the runner")


def test_a_bundle_shows_where_each_memory_came_from(run, test_dsn, committed_scope, tmp_path):
    """27.1 makes the origin mandatory; the review is where it gets read."""
    import json

    with transaction(test_dsn) as cur:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (committed_scope,))
        scope_name = cur.fetchone()["name"]

    path = tmp_path / "sourced.json"
    path.write_text(
        json.dumps(
            [
                {
                    "type": "fact",
                    "title": "a memory with an origin",
                    "content": "the enclosure timed out",
                    "source_reference": "memory/ssd-timeout.md",
                    "directive": "check the enclosure first",
                }
            ]
        ),
        encoding="utf-8",
    )
    _, out = run("import", str(path), "--scope", scope_name)
    bundle = out.splitlines()[0].split()[1]

    code, shown = run("show", "--bundle", bundle)
    assert code == 0
    assert "memory/ssd-timeout.md" in shown
    assert "check the enclosure first" in shown


def test_wrapping_keeps_the_line_breaks_the_writer_put_in():
    """The imported memories are lists of points; filling them makes a wall."""
    wrapped = cli._wrap("- the first point\n- the second point")
    assert wrapped.count("\n") >= 1
    assert "- the first point" in wrapped


def test_the_queue_shows_which_scope_each_proposal_landed_in(run, test_dsn, committed_scope):
    """The scope is the one decision review cannot revise.

    Nothing moves an entity between scopes, so a reviewer who cannot see the
    scope cannot check the only choice that will outlast their verdict.
    """
    _propose(test_dsn, committed_scope, "scope shown in the queue", "the ramp overshot")
    with transaction(test_dsn) as cur:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (committed_scope,))
        scope_name = cur.fetchone()["name"]

    code, out = run("queue")
    assert code == 0
    assert scope_name[:14] in out


def test_import_refuses_a_scope_that_does_not_exist(run, tmp_path):
    path = tmp_path / "memories.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        run("import", str(path), "--scope", "no such scope")


def _scope_name(test_dsn, scope_id):
    with transaction(test_dsn) as cur:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
        return cur.fetchone()["name"]


def test_a_current_state_is_proposed_with_the_memories_it_rests_on(
    run, test_dsn, committed_scope, tmp_path
):
    ground = _propose(test_dsn, committed_scope, "the measurement", "eight seeds, byte identical")
    ground_id = str(ground["target_memory"])

    draft = tmp_path / "state.md"
    draft.write_text("where the scope stands, saying only what its references say", "utf-8")

    code, out = run(
        "state",
        str(draft),
        "--scope",
        _scope_name(test_dsn, committed_scope),
        "--title",
        "current state",
        "--evidence",
        ground_id[:8],
    )
    assert code == 0
    assert "new current state" in out
    assert "resting on 1 memory" in out

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT memory_id, latest_version FROM memory_entity "
            "WHERE scope_id = %s AND type = 'state'",
            (committed_scope,),
        )
        state = cur.fetchone()
        grounds = store.evidence_for(cur, state["latest_version"])
        assert [str(g["memory_id"]) for g in grounds] == [ground_id]


def test_a_second_state_becomes_a_version_of_the_first_not_a_rival(
    run, test_dsn, committed_scope, tmp_path
):
    """Section 14 gives a scope one current state."""
    name = _scope_name(test_dsn, committed_scope)
    draft = tmp_path / "state.md"

    draft.write_text("the first reading", "utf-8")
    run("state", str(draft), "--scope", name, "--title", "current state")

    draft.write_text("the second reading", "utf-8")
    code, out = run("state", str(draft), "--scope", name, "--anyway")
    assert code == 0
    assert "new version of" in out

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM memory_entity WHERE scope_id = %s AND type = 'state'",
            (committed_scope,),
        )
        assert cur.fetchone()["n"] == 1


def test_a_first_state_without_a_title_says_so(run, test_dsn, committed_scope, tmp_path):
    draft = tmp_path / "state.md"
    draft.write_text("a reading", "utf-8")
    with pytest.raises(SystemExit, match="pass --title"):
        run("state", str(draft), "--scope", _scope_name(test_dsn, committed_scope))


def test_an_empty_draft_is_refused(run, test_dsn, committed_scope, tmp_path):
    draft = tmp_path / "state.md"
    draft.write_text("   \n", "utf-8")
    with pytest.raises(SystemExit, match="is empty"):
        run("state", str(draft), "--scope", _scope_name(test_dsn, committed_scope), "--title", "s")


def test_evidence_shows_both_directions(run, test_dsn, committed_scope, tmp_path):
    ground = _propose(test_dsn, committed_scope, "the measurement", "eight seeds")
    ground_id = str(ground["target_memory"])

    draft = tmp_path / "state.md"
    draft.write_text("what the measurement means", "utf-8")
    run(
        "state",
        str(draft),
        "--scope",
        _scope_name(test_dsn, committed_scope),
        "--title",
        "current state",
        "--evidence",
        ground_id[:8],
    )

    code, out = run("evidence", ground_id[:8])
    assert code == 0
    assert "rests on (0" in out
    assert "supports (1)" in out
    assert "current state" in out


def test_the_grounds_are_readable_while_the_state_is_still_waiting(
    run, test_dsn, committed_scope, tmp_path
):
    """A summary that may say only what its references say cannot be reviewed
    without them, and under review it has no active version to read them from."""
    ground = _propose(test_dsn, committed_scope, "the measurement", "eight seeds")
    ground_id = str(ground["target_memory"])

    draft = tmp_path / "state.md"
    draft.write_text("what the measurement means", "utf-8")
    _, out = run(
        "state",
        str(draft),
        "--scope",
        _scope_name(test_dsn, committed_scope),
        "--title",
        "current state",
        "--evidence",
        ground_id[:8],
    )
    proposal_id = out.split("proposal ")[1].split()[0]

    _, shown = run("show", proposal_id)
    assert "resting on (1)" in shown
    assert "the measurement" in shown

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT memory_id FROM memory_entity WHERE scope_id = %s AND type = 'state'",
            (committed_scope,),
        )
        state_id = str(cur.fetchone()["memory_id"])

    _, listed = run("evidence", state_id[:8])
    assert "awaiting review" in listed
    assert "the measurement" in listed


def test_redrafting_a_state_that_is_still_waiting_is_stopped(
    run, test_dsn, committed_scope, tmp_path
):
    """Specification 15.1: the second draft is the same change proposed twice."""
    from mashu.errors import DuplicateProposalError

    name = _scope_name(test_dsn, committed_scope)
    draft = tmp_path / "state.md"
    draft.write_text("the first reading", "utf-8")
    run("state", str(draft), "--scope", name, "--title", "current state")

    draft.write_text("the second reading", "utf-8")
    with pytest.raises(DuplicateProposalError, match="already been proposed"):
        run("state", str(draft), "--scope", name)


# --------------------------------------------------------------------------
# merge, the other door out of provisional (20.1, 20.2)
# --------------------------------------------------------------------------
def test_merging_folds_one_entity_into_another(run, test_dsn, committed_scope):
    source = _propose(test_dsn, committed_scope, "SSD failure analysis", "the drive dropped out")
    target = _propose(test_dsn, committed_scope, "SSD debugging", "the drive dropped out")

    code, out = run(
        "merge",
        str(source["target_memory"])[:8],
        "--into",
        str(target["target_memory"])[:8],
        "--reason",
        "one investigation under two names",
    )
    assert code == 0
    assert "merged into" in out
    assert "1 version(s) moved" in out

    with transaction(test_dsn) as cur:
        folded = store.get_entity(cur, source["target_memory"])
        assert folded["status"] == "merged"
        assert folded["merged_into"] == target["target_memory"]


def test_merging_asks_which_reading_survives_when_both_are_active(run, test_dsn, committed_scope):
    """Choosing between two readings of one concept is a judgement, not a rule."""
    source = _propose(test_dsn, committed_scope, "the first name", "one reading")
    target = _propose(test_dsn, committed_scope, "the second name", "another reading")
    for proposal in (source, target):
        with transaction(test_dsn) as cur:
            proposals.approve(cur, proposal["proposal_id"], reviewer="user", reason="both stand")

    from mashu.errors import MergeError

    with pytest.raises(MergeError, match="name which one survives"):
        run(
            "merge",
            str(source["target_memory"])[:8],
            "--into",
            str(target["target_memory"])[:8],
            "--reason",
            "the same thing",
        )

    with transaction(test_dsn) as cur:
        keep = store.get_entity(cur, source["target_memory"])["active_version"]

    code, out = run(
        "merge",
        str(source["target_memory"])[:8],
        "--into",
        str(target["target_memory"])[:8],
        "--keep-active",
        str(keep)[:8],
        "--reason",
        "the same thing",
    )
    assert code == 0
    assert "superseded" in out

    with transaction(test_dsn) as cur:
        assert store.get_entity(cur, target["target_memory"])["active_version"] == keep


def test_which_side_survives_can_be_named_before_the_versions_exist(run, test_dsn, committed_scope):
    """A merge is planned while the thing being merged is still in review."""
    source = _propose(test_dsn, committed_scope, "the older name", "one reading")
    target = _propose(test_dsn, committed_scope, "the newer name", "another reading")
    for proposal in (source, target):
        with transaction(test_dsn) as cur:
            proposals.approve(cur, proposal["proposal_id"], reviewer="user", reason="both stand")

    code, _ = run(
        "merge",
        str(source["target_memory"])[:8],
        "--into",
        str(target["target_memory"])[:8],
        "--keep-active",
        "into",
        "--reason",
        "one concept",
    )
    assert code == 0

    with transaction(test_dsn) as cur:
        entity = store.get_entity(cur, target["target_memory"])
        assert store.get_version(cur, entity["active_version"])["content"] == "another reading"


def test_the_active_set_prints_as_the_prompt_takes_it(run, test_dsn, committed_scope):
    """The json form is input 2 of session end extraction (16.1)."""
    proposal = _propose(test_dsn, committed_scope, "a standing memory", "the body of it")
    with transaction(test_dsn) as cur:
        proposals.approve(cur, proposal["proposal_id"], reviewer="user", reason="it stands")

    code, out = run("active", "--scope", _scope_name(test_dsn, committed_scope), "--json")
    assert code == 0

    listed = json.loads(out)
    assert [row["title"] for row in listed] == ["a standing memory"]
    assert listed[0]["content"] == "the body of it"
    assert set(listed[0]) == {"memory_id", "type", "title", "content"}


def test_the_active_set_counts_what_it_is_about_to_hand_over(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "a standing memory", "the body of it")
    with transaction(test_dsn) as cur:
        proposals.approve(cur, proposal["proposal_id"], reviewer="user", reason="it stands")

    code, out = run("active", "--scope", _scope_name(test_dsn, committed_scope), "--full")
    assert code == 0
    assert "1 active memory(ies)" in out
    assert "the body of it" in out


def test_the_scope_command_counts_without_gating_anything(run, test_dsn, committed_scope):
    """v0.11 took the readiness manifest and the promotion out of this command.

    They were the shape of work with no judgement in it: declaring which types
    a scope needed, then declaring it open. With the relative cap withdrawn a
    scope answers whether or not anything in it has been adopted, so there is
    nothing left for the declaration to unlock.
    """
    _propose(test_dsn, committed_scope, "still waiting", "not reviewed yet")
    adopted = _propose(test_dsn, committed_scope, "already standing", "reviewed")
    with transaction(test_dsn) as cur:
        proposals.approve(cur, adopted["proposal_id"], reviewer="user", reason="it stands")

    code, out = run("scope")
    assert code == 0
    line = next(row for row in out.splitlines() if _scope_name(test_dsn, committed_scope) in row)
    assert "adopted 1" in line
    assert "unreviewed 1" in line


def test_retype_corrects_the_kind_without_touching_the_content(run, test_dsn, committed_scope):
    """v0.11: the correction that used to take a new entity and a merge."""
    proposal = _propose(
        test_dsn, committed_scope, "the ordering was settled", "one first, then two"
    )
    with transaction(test_dsn) as cur:
        proposals.approve(cur, proposal["proposal_id"], reviewer="user", reason="it stands")
        before = store.get_entity(cur, proposal["target_memory"])["active_version"]

    code, out = run(
        "retype",
        str(proposal["target_memory"])[:8],
        "--to",
        "decision",
        "--reason",
        "the body says the user decided it",
    )
    assert code == 0
    assert "observation -> decision" in out

    with transaction(test_dsn) as cur:
        after = store.get_entity(cur, proposal["target_memory"])
        assert after["type"] == "decision"
        assert after["active_version"] == before
