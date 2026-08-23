"""The review command line (specifications 18, 18.1).

These tests commit rather than roll back, because the CLI opens its own
connection: that is the path being tested, and faking it would leave the part
that actually runs untested. Each test works in its own scope so the shared
database does not make them depend on each other.
"""

from __future__ import annotations

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
