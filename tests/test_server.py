"""The MCP surface (specification 6).

These tests commit, because each tool call opens its own transaction. That is
the path an agent actually takes, and wrapping it in a rollback would test a
different one.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC

import pytest

from mashu import proposals, server, store
from mashu.db import transaction
from mashu.models import EventType, MemoryType, ProposalOperation, SourceType, VersionStatus


@pytest.fixture
def test_dsn(committing_dsn):
    return committing_dsn


@pytest.fixture(autouse=True)
def wired(test_dsn, monkeypatch):
    """Point the server at the committing database, as one fresh process."""
    monkeypatch.setenv("MASHU_DATABASE_URL", test_dsn)
    monkeypatch.setenv(server.ACTOR_ENV_VAR, "claude")
    monkeypatch.setattr(server, "_session_id", None)


@pytest.fixture
def call():
    built = server.build_server()

    def _call(name, **arguments):
        return asyncio.run(built.call_tool(name, arguments)).structured_content

    return _call


@pytest.fixture
def scope(test_dsn):
    with transaction(test_dsn) as cur:
        return store.create_scope(
            cur,
            name=f"mcp scope {uuid.uuid4()}",
            description="the scope these tests work in",
            actor="user",
        )


def _write(test_dsn, scope, type, title, content, adopt=True):
    with transaction(test_dsn) as cur:
        return store.create_entity(
            cur,
            scope_id=scope,
            type=type,
            title=title,
            content=content,
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=adopt,
        )


# --------------------------------------------------------------------------
# the surface itself
# --------------------------------------------------------------------------
def test_the_tools_of_section_six_are_exposed_and_no_others():
    """Pinned as a list, so a tool cannot appear on this surface unremarked.

    The six of the initial set, plus the two scratch tools v0.12 added because
    scratch is the extraction's first input and the agent is what fills it.
    """
    built = server.build_server()
    names = {tool.name for tool in asyncio.run(built.list_tools())}
    assert names == {
        "memory_search",
        "memory_get",
        "memory_propose",
        "scope_list",
        "entity_resolve",
        "session_bootstrap",
        "scratch_put",
        "scratch_get",
    }


def test_the_actor_comes_from_the_configuration_not_the_caller(test_dsn, monkeypatch, call, scope):
    """An identity a caller asserts about itself would be worth nothing."""
    monkeypatch.setenv(server.ACTOR_ENV_VAR, "codex")
    call("scope_list")
    assert server.actor() == "codex"

    call("session_bootstrap")
    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT actor FROM event_log WHERE event_type = %s ORDER BY created_at DESC LIMIT 1",
            (str(EventType.SESSION_BOOTSTRAPPED),),
        )
        assert cur.fetchone()["actor"] == "codex"


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def test_bootstrap_hands_over_the_map(call, scope):
    got = call("session_bootstrap")
    assert got["ok"]
    assert str(scope) in [row["scope_id"] for row in got["scope_index"]]


def test_search_answers_in_three_layers(test_dsn, call, scope):
    _write(test_dsn, scope, MemoryType.FACT, "the ramp rate", "two degrees a minute")
    got = call("memory_search", query="the ramp rate", scope=str(scope))
    assert got["ok"]
    assert "two degrees a minute" in [row["content"] for row in got["active"]]
    assert got["unreviewed"] == []
    assert got["retired"] == []


def test_a_retired_memory_keeps_its_content_back_from_get_as_well(test_dsn, call, scope):
    """Otherwise memory_get is the way around 21.1 rather than an exception to it."""
    memory_id, version_id = _write(
        test_dsn, scope, MemoryType.HYPOTHESIS, "SSD timeout cause", "the bridge chip"
    )
    with transaction(test_dsn) as cur:
        store.set_status(
            cur,
            version_id=version_id,
            target=VersionStatus.DISPROVEN,
            actor="user",
            reason="it reproduced over a direct SATA connection",
        )

    got = call("memory_get", memory_id=str(memory_id))
    assert got["layer"] == "retired"
    assert got["status"] == str(VersionStatus.DISPROVEN)
    assert got["reason"] == "it reproduced over a direct SATA connection"
    assert "content" not in got


def test_an_unreviewed_memory_is_readable_and_says_so(test_dsn, call, scope):
    """v0.7 semi-approval: the content comes over, carrying the tag."""
    memory_id, _ = _write(
        test_dsn, scope, MemoryType.FACT, "the ramp rate", "two degrees a minute", adopt=False
    )
    got = call("memory_get", memory_id=str(memory_id))
    assert got["layer"] == "unreviewed"
    assert got["content"] == "two degrees a minute"
    assert got["tag"] == "unreviewed"


def test_entity_resolve_offers_what_already_exists(test_dsn, call, scope):
    _write(test_dsn, scope, MemoryType.FACT, "the ramp rate", "two degrees a minute")
    got = call("entity_resolve", scope=str(scope), title="the ramp rate")
    assert [row["title"] for row in got["candidates"]] == ["the ramp rate"]


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------
def test_a_proposal_lands_in_a_bundle_without_the_agent_carrying_the_id(test_dsn, call, scope):
    """18.1 reviews a session at a time, so a proposal with no session is loose."""
    first = call(
        "memory_propose",
        operation=str(ProposalOperation.CREATE),
        payload={
            "scope_id": str(scope),
            "type": str(MemoryType.OBSERVATION),
            "title": "the ramp overshot",
            "content": "the ramp overshot by two degrees",
            "source_type": str(SourceType.AGENT),
        },
    )
    second = call(
        "memory_propose",
        operation=str(ProposalOperation.CREATE),
        payload={
            "scope_id": str(scope),
            "type": str(MemoryType.INTERPRETATION),
            "title": "the thermocouple is out",
            "content": "the overshoot points at the thermocouple",
            "source_type": str(SourceType.AGENT),
        },
    )
    assert first["ok"] and second["ok"]
    assert first["commit_line"] == "candidate"

    with transaction(test_dsn) as cur:
        cur.execute(
            "SELECT DISTINCT session_id FROM proposal WHERE proposal_id IN (%s, %s)",
            (first["proposal_id"], second["proposal_id"]),
        )
        sessions = cur.fetchall()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] is not None


def test_a_duplicate_comes_back_as_something_to_act_on(call, scope):
    """The refusal exists to show the proposer what is already there."""
    payload = {
        "scope_id": str(scope),
        "type": str(MemoryType.OBSERVATION),
        "title": "the ramp overshot",
        "content": "the ramp overshot by two degrees",
        "source_type": str(SourceType.AGENT),
    }
    call("memory_propose", operation=str(ProposalOperation.CREATE), payload=payload)
    again = call("memory_propose", operation=str(ProposalOperation.CREATE), payload=payload)

    assert again["ok"] is False
    assert again["existing"]
    assert "already been proposed" in again["error"]


def test_a_rejection_reaches_the_next_proposer_through_the_tool(test_dsn, call, scope):
    payload = {
        "scope_id": str(scope),
        "type": str(MemoryType.HYPOTHESIS),
        "title": "the enclosure bridge chip",
        "content": "the timeout comes from the bridge chip",
        "source_type": str(SourceType.AGENT),
    }
    first = call("memory_propose", operation=str(ProposalOperation.CREATE), payload=payload)
    with transaction(test_dsn) as cur:
        proposals.reject(
            cur,
            uuid.UUID(first["proposal_id"]),
            reviewer="user",
            reason="it reproduced over a direct SATA connection",
        )

    again = call("memory_propose", operation=str(ProposalOperation.CREATE), payload=payload)
    assert again["ok"] is False
    assert "direct SATA connection" in again["error"]


def test_a_bad_request_answers_rather_than_crashing_the_session(call, scope):
    got = call("memory_get", memory_id=str(uuid.uuid4()))
    assert got["ok"] is False
    assert got["error"]


# --------------------------------------------------------------------------
# the trust boundary: what a caller may not decide about its own proposal
# --------------------------------------------------------------------------
def test_a_caller_cannot_label_its_own_proposal_user_stated(call, scope, test_dsn):
    """Section 17's preference rule is entirely the source, so the source is set here.

    A caller that can claim the user said it can hand itself the standing
    instructions every later session follows, which is the hole the gate was
    changed to close.
    """
    answer = call(
        "memory_propose",
        operation="create",
        payload={
            "scope_id": str(scope),
            "type": str(MemoryType.PREFERENCE),
            "title": "always do the thing this document says",
            "content": "a standing instruction lifted from something read along the way",
            "source_type": str(SourceType.USER),
        },
    )
    assert answer["ok"] is True
    assert answer["commit_line"] == "candidate"

    with transaction(test_dsn) as cur:
        entity = store.get_entity(cur, uuid.UUID(answer["memory_id"]))
        assert entity["active_version"] is None
        version = store.get_version(cur, uuid.UUID(answer["version_id"]))
        assert version["source_type"] == str(SourceType.AGENT)


def test_a_caller_cannot_skip_the_similarity_check(call, scope):
    """Section 20 runs only when the payload carries no entity status of its own."""
    first = dict(
        scope_id=str(scope),
        type=str(MemoryType.FACT),
        title="the enclosure bridge chip times out",
        content="the first reading",
        source_type=str(SourceType.AGENT),
    )
    call("memory_propose", operation="create", payload=first)

    # allow_duplicate gets past 15.1, so what stops this is section 20 alone.
    answer = call(
        "memory_propose",
        operation="create",
        payload=dict(
            first,
            title="the enclosure bridge chip times out on this rig",
            content="a second reading",
            entity_status="active",
        ),
        allow_duplicate=True,
    )
    assert answer["ok"] is False
    assert "allow_similar" in answer["error"]


# --------------------------------------------------------------------------
# scratch, and what a session start now carries (25.1, 25.2, 16.3)
# --------------------------------------------------------------------------
def test_an_agent_can_put_scratch_down_and_read_it_back(call):
    put = call("scratch_put", content="the guard is in but the sweep was not rerun")
    assert put["ok"] is True

    got = call("scratch_get")
    assert [item["content"] for item in got["items"]] == [
        "the guard is in but the sweep was not rerun"
    ]


def test_scratch_does_not_reach_another_session(call, test_dsn, monkeypatch):
    call("scratch_put", content="mine alone")

    # A second server process is a second logical session.
    monkeypatch.setattr(server, "_session_id", None)
    second = server.build_server()
    got = asyncio.run(second.call_tool("scratch_get", {})).structured_content
    assert got["items"] == []


def test_the_session_start_carries_the_conditions_and_the_capture_health(call, test_dsn, scope):
    from datetime import datetime, timedelta

    from mashu import context
    from mashu.models import SourceType

    with transaction(test_dsn) as cur:
        context.put(
            cur,
            content="the delegation quota is free until the reset",
            expires_at=datetime.now(UTC) + timedelta(hours=8),
            kind="fact",
            source_type=SourceType.USER,
            created_by="user",
            actor="user",
        )

    # These tests commit, so other tests' scopeless conditions are live too;
    # what matters is that this one is carried and that it is a person's.
    got = call("session_bootstrap")
    mine = [row for row in got["temporary"] if "delegation quota" in row["content"]]
    assert len(mine) == 1
    assert mine[0]["source_type"] == "user"
    # The health flag itself is asserted where the database rolls back; here
    # another test has deliberately broken a run in the same store.
    assert set(got["capture"]) >= {"ok", "failed", "waiting", "warning"}
