"""The MCP surface (specification 6).

These tests commit, because each tool call opens its own transaction. That is
the path an agent actually takes, and wrapping it in a rollback would test a
different one.
"""

from __future__ import annotations

import asyncio
import uuid

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
def test_the_six_tools_of_section_six_are_exposed():
    built = server.build_server()
    names = {tool.name for tool in asyncio.run(built.list_tools())}
    assert names == {
        "memory_search",
        "memory_get",
        "memory_propose",
        "scope_list",
        "entity_resolve",
        "session_bootstrap",
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
