"""The review command line (specifications 18, 18.1).

These tests commit rather than roll back, because the CLI opens its own
connection: that is the path being tested, and faking it would leave the part
that actually runs untested. Each test works in its own scope so the shared
database does not make them depend on each other.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from mashu import cli, context, proposals, routing, runs, store
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

    code, out = run("admin", "queue")
    assert code == 0
    assert str(session_id)[:8] in out
    assert out.index("cli observation") < out.index("cli decision"), (
        "the bundle has to read grounds before conclusions"
    )


def test_show_prints_the_proposed_text(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "cli show", "the enclosure timed out again")
    code, out = run("inspect", str(proposal["proposal_id"])[:8])
    assert code == 0
    assert "the enclosure timed out again" in out


def test_approving_by_prefix_makes_it_current(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "cli approve", "the ramp is half a degree")
    code, out = run("admin", "approve", str(proposal["proposal_id"])[:8], "--reason", "checked")
    assert code == 0

    with transaction(test_dsn) as cur:
        entity = store.get_entity(cur, proposal["target_memory"])
    assert entity["active_version"] == proposal["applied_version"]


def test_rejecting_records_the_reason(run, test_dsn, committed_scope):
    proposal = _propose(test_dsn, committed_scope, "cli reject", "an uncalibrated reading")
    code, _ = run(
        "admin", "reject", str(proposal["proposal_id"])[:8], "--reason", "wrong thermocouple"
    )
    assert code == 0

    with transaction(test_dsn) as cur:
        decided = proposals.get(cur, proposal["proposal_id"])
    assert decided["decision_reason"] == "wrong thermocouple"


def test_an_ambiguous_prefix_stops_rather_than_guessing(run, test_dsn, committed_scope):
    """Deciding the wrong proposal is not recoverable by re-running the command."""
    _propose(test_dsn, committed_scope, "cli ambiguous one", "some content")
    _propose(test_dsn, committed_scope, "cli ambiguous two", "other content")
    with pytest.raises(SystemExit) as caught:
        run("inspect", "")
    assert "use more characters" in str(caught.value)


def test_an_unknown_prefix_stops(run):
    with pytest.raises(SystemExit):
        run("inspect", "ffffffffff")


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
    code, out = run("find", "searchable ramp")
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
    code, out = run("admin", "queue")
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

    code, out = run("admin", "backfill")
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

    code, out = run("admin", "import", str(path), "--scope", scope_name)
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

    code, out = run("admin", "import", str(path), "--scope", scope_name)
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

    code, out = run("inspect", "--bundle", str(session_id)[:8])
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
    _, out = run("admin", "import", str(path), "--scope", scope_name)
    bundle = out.splitlines()[0].split()[1]

    code, shown = run("inspect", "--bundle", bundle)
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

    code, out = run("admin", "queue")
    assert code == 0
    assert scope_name[:14] in out


def test_import_refuses_a_scope_that_does_not_exist(run, tmp_path):
    path = tmp_path / "memories.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        run("admin", "import", str(path), "--scope", "no such scope")


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
        "admin",
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
    run("admin", "state", str(draft), "--scope", name, "--title", "current state")

    draft.write_text("the second reading", "utf-8")
    code, out = run("admin", "state", str(draft), "--scope", name, "--anyway")
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
        run("admin", "state", str(draft), "--scope", _scope_name(test_dsn, committed_scope))


def test_an_empty_draft_is_refused(run, test_dsn, committed_scope, tmp_path):
    draft = tmp_path / "state.md"
    draft.write_text("   \n", "utf-8")
    with pytest.raises(SystemExit, match="is empty"):
        run(
            "admin",
            "state",
            str(draft),
            "--scope",
            _scope_name(test_dsn, committed_scope),
            "--title",
            "s",
        )


def test_evidence_shows_both_directions(run, test_dsn, committed_scope, tmp_path):
    ground = _propose(test_dsn, committed_scope, "the measurement", "eight seeds")
    ground_id = str(ground["target_memory"])

    draft = tmp_path / "state.md"
    draft.write_text("what the measurement means", "utf-8")
    run(
        "admin",
        "state",
        str(draft),
        "--scope",
        _scope_name(test_dsn, committed_scope),
        "--title",
        "current state",
        "--evidence",
        ground_id[:8],
    )

    code, out = run("admin", "evidence", ground_id[:8])
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
        "admin",
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

    _, shown = run("inspect", proposal_id)
    assert "resting on (1)" in shown
    assert "the measurement" in shown

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT memory_id FROM memory_entity WHERE scope_id = %s AND type = 'state'",
            (committed_scope,),
        )
        state_id = str(cur.fetchone()["memory_id"])

    _, listed = run("admin", "evidence", state_id[:8])
    assert "awaiting review" in listed
    assert "the measurement" in listed


def test_redrafting_a_state_that_is_still_waiting_is_stopped(
    run, test_dsn, committed_scope, tmp_path
):
    """Specification 15.1: the second draft is the same change proposed twice.

    The refusal reaches the user as its own sentence. It exists to show the
    proposer what is already waiting, and a traceback buries that under a stack
    they cannot act on.
    """
    name = _scope_name(test_dsn, committed_scope)
    draft = tmp_path / "state.md"
    draft.write_text("the first reading", "utf-8")
    run("admin", "state", str(draft), "--scope", name, "--title", "current state")

    draft.write_text("the second reading", "utf-8")
    code, _ = run("admin", "state", str(draft), "--scope", name)
    assert code == 1


# --------------------------------------------------------------------------
# merge, the other door out of provisional (20.1, 20.2)
# --------------------------------------------------------------------------
def test_merging_folds_one_entity_into_another(run, test_dsn, committed_scope):
    source = _propose(test_dsn, committed_scope, "SSD failure analysis", "the drive dropped out")
    target = _propose(test_dsn, committed_scope, "SSD debugging", "the drive dropped out")

    code, out = run(
        "admin",
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

    code, _ = run(
        "admin",
        "merge",
        str(source["target_memory"])[:8],
        "--into",
        str(target["target_memory"])[:8],
        "--reason",
        "the same thing",
    )
    assert code == 1

    with transaction(test_dsn) as cur:
        keep = store.get_entity(cur, source["target_memory"])["active_version"]

    code, out = run(
        "admin",
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
        "admin",
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
        "admin",
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


# --------------------------------------------------------------------------
# the user explicit trigger, which had no way in (16, 17)
# --------------------------------------------------------------------------
def test_what_the_user_states_lands_without_waiting(run, test_dsn, committed_scope):
    code, out = run(
        "remember",
        "the body of the rule, with why and how to apply",
        "--scope",
        _scope_name(test_dsn, committed_scope),
        "--type",
        "preference",
        "--title",
        "a rule the user stated",
    )
    assert code == 0
    assert "active" in out
    assert "change stated by the user" in out

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT memory_id, active_version FROM memory_entity "
            "WHERE scope_id = %s AND title = %s",
            (committed_scope, "a rule the user stated"),
        )
        entity = cur.fetchone()
        assert entity["active_version"] is not None
        version = store.get_version(cur, entity["active_version"])
        assert version["source_type"] == "user"


def test_a_similar_title_stops_it_rather_than_writing_beside_it(
    run, test_dsn, committed_scope, capsys
):
    """Whether this is a second memory or a new version of the first is a judgement."""
    name = _scope_name(test_dsn, committed_scope)
    run(
        "remember",
        "the first reading",
        "--scope",
        name,
        "--type",
        "fact",
        "--title",
        "the enclosure bridge chip times out",
    )

    with pytest.raises(SystemExit, match="--anyway"):
        run(
            "remember",
            "the second reading",
            "--scope",
            name,
            "--type",
            "fact",
            "--title",
            "the enclosure bridge chip times out",
        )


def test_recording_beside_a_similar_one_waits_for_review(run, test_dsn, committed_scope):
    """17 checks the entity status before it checks who said it."""
    name = _scope_name(test_dsn, committed_scope)
    run("remember", "one", "--scope", name, "--type", "fact", "--title", "the drive drops out")

    code, out = run(
        "remember",
        "another",
        "--scope",
        name,
        "--type",
        "fact",
        "--title",
        "the drive drops out",
        "--anyway",
    )
    assert code == 0
    assert "pending" in out


def test_an_empty_body_is_refused(run, test_dsn, committed_scope):
    with pytest.raises(SystemExit, match="nothing to remember"):
        run(
            "remember",
            "   ",
            "--scope",
            _scope_name(test_dsn, committed_scope),
            "--type",
            "fact",
            "--title",
            "an empty one",
        )


def test_a_condition_with_a_window_is_not_a_memory(run, test_dsn, committed_scope):
    """25.2: it takes no type and no title, and no review stands between it and use."""
    code, out = run(
        "remember",
        "the cluster is in maintenance",
        "--until",
        "2d",
        "--scope",
        _scope_name(test_dsn, committed_scope),
    )
    assert code == 0
    assert "no review" in out

    with transaction(test_dsn) as cur:
        live = context.live(cur, scopes=[committed_scope])
        assert [row["content"] for row in live] == ["the cluster is in maintenance"]
        assert live[0]["source_type"] == "user"

        cur.execute(
            "SELECT count(*) AS n FROM memory_entity WHERE scope_id = %s", (committed_scope,)
        )
        assert cur.fetchone()["n"] == 0


def test_the_expiry_is_printed_with_its_zone(run, test_dsn, committed_scope):
    """An expiry read differently by different readers is what 25.2 refuses."""
    _, out = run("remember", "a passing condition", "--until", "6h")
    assert "UTC+" in out or "UTC-" in out


def test_a_moment_that_reads_two_ways_is_refused(run, test_dsn):
    with pytest.raises(SystemExit, match="resolves the same way twice"):
        run("remember", "something", "--until", "tomorrow morning")


def test_a_memory_still_needs_its_type_and_title(run, test_dsn, committed_scope):
    with pytest.raises(SystemExit, match="--until instead"):
        run("remember", "a lasting rule", "--scope", _scope_name(test_dsn, committed_scope))


def test_runs_reports_that_capture_stopped(run, test_dsn):
    with transaction(test_dsn) as cur:
        r = runs.enqueue(
            cur,
            source_cli="claude",
            external_session_id="cli-1",
            transcript_digest="cli-d1",
            extractor_version="v1",
        )
        while True:
            cur.execute("UPDATE extraction_run SET next_retry_at = now() - interval '1 minute'")
            if not runs.claim(cur):
                break
            if runs.failed(cur, run_id=r["run_id"], error="the model timed out")["state"] == (
                "failed"
            ):
                break

    code, out = run("admin", "runs")
    assert code == 0
    assert "failed 1" in out
    assert "Nothing new is reaching the store" in out


def test_enqueue_claims_a_transcript_once(run, test_dsn, tmp_path):
    """The hook may fire twice, and the sweeper covers the times it does not."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"a": 1}\n', "utf-8")

    code, first = run("admin", "enqueue", str(transcript), "--cli", "claude", "--session", "ext-1")
    assert code == 0
    assert "queued" in first

    _, second = run("admin", "enqueue", str(transcript), "--cli", "claude", "--session", "ext-1")
    assert first.split()[0] == second.split()[0]

    with transaction(test_dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM extraction_run WHERE external_session_id = 'ext-1'")
        assert cur.fetchone()["n"] == 1


def test_a_changed_transcript_is_a_new_run(run, test_dsn, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"a": 1}\n', "utf-8")
    run("admin", "enqueue", str(transcript), "--cli", "claude", "--session", "ext-2")

    transcript.write_text('{"a": 1}\n{"b": 2}\n', "utf-8")
    run("admin", "enqueue", str(transcript), "--cli", "claude", "--session", "ext-2")

    with transaction(test_dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM extraction_run WHERE external_session_id = 'ext-2'")
        assert cur.fetchone()["n"] == 2


def test_a_missing_transcript_does_not_fail_the_hook(run, tmp_path):
    """A non-zero exit here is a visible error for something nobody asked for."""
    code, _ = run(
        "admin", "enqueue", str(tmp_path / "gone.jsonl"), "--cli", "claude", "--session", "ext-3"
    )
    assert code == 0


# --------------------------------------------------------------------------
# one sitting (30 段 B, 段 C)
# --------------------------------------------------------------------------
def _session(test_dsn, name):
    with transaction(test_dsn) as cur:
        cur.execute(
            "INSERT INTO agent_session (agent, source_cli, external_session_id) "
            "VALUES ('claude', 'claude', %s) RETURNING session_id",
            (name,),
        )
        return cur.fetchone()["session_id"]


def test_a_sitting_approves_rejects_and_puts_off_in_one_pass(test_dsn, run, committed_scope):
    """段 C: review is optional now, so one sitting has to be enough."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    for title in ("最初の項目", "二番目の項目", "三番目の項目"):
        _propose(test_dsn, committed_scope, title, f"{title}の本文", session_id=session_id)

    code, out = run(
        "review",
        "--bundle",
        str(session_id)[:8],
        "--batch",
        "all; r 2 根拠が薄い; s 3 明日確かめる",
    )
    assert code == 0
    assert "approved" in out and "declined" in out and "put off" in out

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT p.status, p.review_note, p.payload ->> 'title' AS title FROM proposal p "
            "WHERE p.session_id = %s ORDER BY p.seq",
            (session_id,),
        )
        rows = cur.fetchall()
    by_title = {r["title"]: r for r in rows}
    assert by_title["最初の項目"]["status"] == "approved"
    assert by_title["二番目の項目"]["status"] == "declined"
    assert by_title["三番目の項目"]["status"] == "pending"
    assert by_title["三番目の項目"]["review_note"] == "明日確かめる"


def test_putting_something_off_without_saying_why_is_refused(test_dsn, run, committed_scope):
    """A skip that leaves no trace is the same row state as never having looked."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    _propose(test_dsn, committed_scope, "何かの項目", "本文", session_id=session_id)

    code, _ = run("review", "--bundle", str(session_id)[:8], "--batch", "s 1")
    assert code == 1


def test_a_bundle_everyone_has_already_put_off_stops_leading_the_queue(
    test_dsn, run, committed_scope
):
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    _propose(test_dsn, committed_scope, "先送りする項目", "本文", session_id=session_id)
    run("review", "--bundle", str(session_id)[:8], "--batch", "s 1 あとで")

    code, out = run("review", "--batch", "q")
    assert code == 0
    assert "先送りする項目" not in out


def test_the_user_can_retire_something_themselves(test_dsn, run, committed_scope):
    """16.1 named the user's own statement as a trigger and gave it no entrance."""
    with transaction(test_dsn) as cur:
        memory_id, version_id = store.create_entity(
            cur,
            scope_id=committed_scope,
            type=MemoryType.TASK,
            title=f"終わる作業 {uuid.uuid4()}",
            content="いつか終わる",
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
            adopt=True,
        )

    code, out = run("retire", str(memory_id)[:8], "completed", "--reason", "この夜に終えた")
    assert code == 0
    with transaction(test_dsn) as cur:
        assert store.get_version(cur, version_id)["status"] == "completed"


def test_the_user_disproving_something_is_recorded_as_the_review_it_is(
    test_dsn, run, committed_scope
):
    """17 holds disproven for human review whoever asks; the person here is it."""
    with transaction(test_dsn) as cur:
        memory_id, version_id = store.create_entity(
            cur,
            scope_id=committed_scope,
            type=MemoryType.HYPOTHESIS,
            title=f"覆る仮説 {uuid.uuid4()}",
            content="ブリッジチップが原因",
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=True,
        )

    code, out = run("retire", str(memory_id)[:8], "disproven", "--reason", "直結でも再現した")
    assert code == 0
    with transaction(test_dsn) as cur:
        assert store.get_version(cur, version_id)["status"] == "disproven"
        cur.execute(
            "SELECT reviewer, decision_reason FROM proposal WHERE target_memory = %s", (memory_id,)
        )
        row = cur.fetchone()
    assert row["reviewer"] == "user"
    assert "stated at the terminal" in row["decision_reason"]


def test_a_route_is_stated_and_a_held_transcript_is_released_when_it_appears(
    test_dsn, run, committed_scope
):
    with transaction(test_dsn) as cur:
        cur.execute("SELECT name FROM scope WHERE scope_id = %s", (committed_scope,))
        name = cur.fetchone()["name"]
        held = runs.enqueue(
            cur,
            source_cli="claude",
            external_session_id=f"held-{uuid.uuid4()}",
            transcript_digest=str(uuid.uuid4()),
            extractor_version="v1",
            cwd="/tmp/routed/project",
        )
        runs.held(cur, run_id=held["run_id"], note="no scope route")

    code, out = run("route", "--add", "/tmp/routed", "--scope", name)
    assert code == 0
    assert "released 1 held transcript" in out

    with transaction(test_dsn) as cur:
        cur.execute("DELETE FROM extraction_run WHERE run_id = %s", (held["run_id"],))


def test_a_directory_can_be_routed_to_no_scope_on_purpose(test_dsn, run):
    """The third answer, reachable from the terminal (16.3, migration 0017).

    The schema, the resolver and the argument all carried it; the command body
    did not, so "seen, and deliberately not captured" could not be said. A run
    from such a directory stayed held, and the health line it lit rides into
    every session opening — a warning that never goes out is one nobody reads.
    """
    with transaction(test_dsn) as cur:
        held = runs.enqueue(
            cur,
            source_cli="claude",
            external_session_id=f"held-{uuid.uuid4()}",
            transcript_digest=str(uuid.uuid4()),
            extractor_version="v1",
            cwd="/tmp/scratchpad/throwaway",
        )
        runs.held(cur, run_id=held["run_id"], note="no scope route")

    code, out = run("route", "--ignore", "/tmp/scratchpad")
    assert code == 0
    assert "no scope, on purpose" in out
    assert "released 1 held transcript" in out

    with transaction(test_dsn) as cur:
        scope_id, on_purpose = routing.resolve(cur, "/tmp/scratchpad/throwaway")
        assert scope_id is None
        assert on_purpose is True

        cur.execute("DELETE FROM extraction_run WHERE run_id = %s", (held["run_id"],))
        routing.remove(cur, path_prefix="/tmp/scratchpad")


def test_a_scope_can_be_created_and_routed_in_one_go(test_dsn, run):
    """Section 7 reserves this for the user, who until now had no way either.

    A session run in an unmapped directory is held for a scope that has to be
    made by hand, so the ledger's one human-only write was the one with no
    entrance at all.
    """
    name = f"新しい案件 {uuid.uuid4()}"
    with transaction(test_dsn) as cur:
        held = runs.enqueue(
            cur,
            source_cli="claude",
            external_session_id=f"held-{uuid.uuid4()}",
            transcript_digest=str(uuid.uuid4()),
            extractor_version="v1",
            cwd="/tmp/新しい案件/src",
        )
        runs.held(cur, run_id=held["run_id"], note="no scope route")

    code, out = run(
        "scope", "--add", name, "--about", "その案件の設計と運用", "--route", "/tmp/新しい案件"
    )
    assert code == 0
    assert "released 1 held transcript" in out

    with transaction(test_dsn) as cur:
        scope_id, on_purpose = routing.resolve(cur, "/tmp/新しい案件/src")
        assert scope_id is not None and not on_purpose
        cur.execute("SELECT description FROM scope WHERE scope_id = %s", (scope_id,))
        assert cur.fetchone()["description"] == "その案件の設計と運用"

        cur.execute("DELETE FROM extraction_run WHERE run_id = %s", (held["run_id"],))
        routing.remove(cur, path_prefix="/tmp/新しい案件")


def test_a_scope_made_without_a_description_says_what_that_costs(test_dsn, run):
    """Detection matches on the name and that line, and the index is that line."""
    code, out = run("scope", "--add", f"名無し {uuid.uuid4()}")
    assert code == 0
    assert "scope detection has only the name" in out


# --------------------------------------------------------------------------
# the sitting, driven one keystroke at a time (18.1)
# --------------------------------------------------------------------------
class _Terminal:
    """A stand-in for the reader: a keystroke each time one is asked for."""

    def __init__(self, keys):
        self.keys = list(keys)

    def isatty(self):
        return True

    def key(self):
        return self.keys.pop(0) if self.keys else "q"


@pytest.fixture
def sitting(test_dsn, monkeypatch, capsys):
    """Run 'mashu review' as if a person were pressing keys at it."""

    def _sit(keys, typed=(), *argv):
        terminal = _Terminal(keys)
        answers = list(typed)
        monkeypatch.setattr(cli.sys, "stdin", terminal)
        monkeypatch.setattr(cli, "_getkey", terminal.key)
        monkeypatch.setattr("builtins.input", lambda *_: answers.pop(0) if answers else "")
        code = cli.main(["--dsn", test_dsn, "review", *argv])
        return code, capsys.readouterr().out

    return _sit


def _statuses(test_dsn, session_id) -> dict[str, str]:
    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT status, payload ->> 'title' AS title FROM proposal "
            "WHERE session_id = %s ORDER BY seq",
            (session_id,),
        )
        return {row["title"]: row["status"] for row in cur.fetchall()}


def test_a_sitting_decides_one_item_per_keystroke(test_dsn, sitting, committed_scope):
    """The cost 18.1 measured is context switches; the cost left over was typing."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    for title in ("一つ目", "二つ目", "三つ目"):
        _propose(test_dsn, committed_scope, title, f"{title}の本文", session_id=session_id)

    # open the list, approve the first, turn the second down, approve the rest
    code, out = sitting(["enter", "y", "r", "a"], ["根拠が薄い"], "--bundle", str(session_id)[:8])
    assert code == 0

    assert _statuses(test_dsn, session_id) == {
        "一つ目": "approved",
        "二つ目": "declined",
        "三つ目": "approved",
    }
    assert "1 approved, 1 declined" in out or "2 approved, 1 declined" in out


def test_the_arrows_move_through_a_bundle_without_deciding_anything(
    test_dsn, sitting, committed_scope
):
    """Reading is not deciding. Nothing is settled until a key that settles it."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    for title in ("見るだけ 1", "見るだけ 2"):
        _propose(test_dsn, committed_scope, title, f"{title}の本文", session_id=session_id)

    code, _ = sitting(
        ["down", "enter", "down", "up", "left", "q"], (), "--bundle", str(session_id)[:8]
    )
    assert code == 0
    assert set(_statuses(test_dsn, session_id).values()) == {"pending"}


def test_leaving_in_the_middle_keeps_what_was_already_decided(test_dsn, sitting, committed_scope):
    """One transaction per item is what makes a half-finished sitting worth having."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    for title in ("決める分", "残す分"):
        _propose(test_dsn, committed_scope, title, f"{title}の本文", session_id=session_id)

    code, out = sitting(["enter", "y", "q"], (), "--bundle", str(session_id)[:8])
    assert code == 0
    assert _statuses(test_dsn, session_id) == {"決める分": "approved", "残す分": "pending"}
    assert "'mashu review' opens on the rest" in out


def test_a_reason_left_empty_cancels_the_decision_instead_of_making_it(
    test_dsn, sitting, committed_scope
):
    """18.1 makes the reason mandatory, so no reason has to mean no decision."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    _propose(test_dsn, committed_scope, "思い直す分", "本文", session_id=session_id)

    code, out = sitting(["enter", "r", "q"], [""], "--bundle", str(session_id)[:8])
    assert code == 0
    assert _statuses(test_dsn, session_id) == {"思い直す分": "pending"}
    assert "never mind" in out


def test_all_widens_the_bundles_and_not_only_the_items_inside_them(test_dsn, run, committed_scope):
    """The advice to pass --all was showing the same nothing to whoever took it.

    A bundle whose every item is put off has deferred == count, so the filter
    that keeps settled bundles out of the queue was keeping this one out too,
    and the only flag that could have brought it back only widened the items
    within a bundle already chosen.
    """
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    _propose(test_dsn, committed_scope, "先送りした分", "本文", session_id=session_id)
    run("review", "--bundle", str(session_id)[:8], "--batch", "s 1 あとで")

    with transaction(test_dsn) as cur:
        waiting = proposals.session_queue(cur)
        plain = cli._wanted(cur, waiting, SimpleNamespace(bundle=None, all=False))
        widened = cli._wanted(cur, waiting, SimpleNamespace(bundle=None, all=True))

    assert session_id not in [b["session_id"] for b in plain]
    assert session_id in [b["session_id"] for b in widened]


def _place(test_dsn, session_id) -> int:
    """Where a bundle sits in the list a sitting opens on."""
    with transaction(test_dsn) as cur:
        shelf = cli._wanted(
            cur, proposals.session_queue(cur), SimpleNamespace(bundle=None, all=False)
        )
    return [b["session_id"] for b in shelf].index(session_id)


def test_the_list_of_bundles_lets_a_heavy_one_be_passed_over(test_dsn, sitting, committed_scope):
    """Which bundle is oldest is an accident; the reader's ten minutes are not."""
    heavy = _session(test_dsn, f"sit-{uuid.uuid4()}")
    light = _session(test_dsn, f"sit-{uuid.uuid4()}")
    for title in ("重い 1", "重い 2", "重い 3"):
        _propose(test_dsn, committed_scope, title, f"{title}の本文", session_id=heavy)
    _propose(test_dsn, committed_scope, "軽い 1", "本文", session_id=light)

    keys = ["down"] * _place(test_dsn, light) + ["enter", "a", "q"]
    code, _ = sitting(keys)
    assert code == 0

    assert _statuses(test_dsn, light) == {"軽い 1": "approved"}
    assert set(_statuses(test_dsn, heavy).values()) == {"pending"}


def test_finishing_a_bundle_comes_back_to_the_list_with_what_it_came_to(
    test_dsn, sitting, committed_scope
):
    """A tally the next screen wipes is a tally nobody reads, so the list carries it."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    _propose(test_dsn, committed_scope, "片付ける分", "本文", session_id=session_id)

    keys = ["down"] * _place(test_dsn, session_id) + ["enter", "a", "q"]
    code, out = sitting(keys)
    assert code == 0
    assert _statuses(test_dsn, session_id) == {"片付ける分": "approved"}
    assert "1 approved" in out.rsplit("bundle(s) waiting", 1)[-1]


def test_a_whole_bundle_can_be_approved_without_opening_it(test_dsn, sitting, committed_scope):
    """18.1 expects passing the bundle to be the common case, so it costs one key."""
    session_id = _session(test_dsn, f"sit-{uuid.uuid4()}")
    for title in ("まとめて 1", "まとめて 2"):
        _propose(test_dsn, committed_scope, title, f"{title}の本文", session_id=session_id)

    keys = ["down"] * _place(test_dsn, session_id) + ["a", "q"]
    code, _ = sitting(keys)
    assert code == 0
    assert set(_statuses(test_dsn, session_id).values()) == {"approved"}
