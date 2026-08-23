"""The MCP server (specification 6).

Every agent reaches the knowledge state through this one surface. The reason
the gateway is MCP rather than a per-agent adapter is the success condition in
section 31: independence from the kind of agent has to hold at the connection
level, not by writing the same client three times.

Three things are settled here that the specification leaves to the
implementation.

**Who the actor is.** Section 4 gives the agent a narrower permission set than
the user, so every call needs an identity. The agent does not supply it: an
identity a caller asserts about itself is worth nothing, and asking for it on
every call would only add a field to lie in. It comes from the environment the
client was configured with, which is the user's own configuration file.

**What a session is.** Section 18.1 reviews a session's proposals as one
bundle, ordered grounds before conclusions, so proposals need a session to
belong to. The server opens one row on first use and holds it for the life of
the process. Under stdio the client starts one process per session, so the
process is the session; the agent is never asked to carry the id around,
because a bundle that depends on the agent remembering to pass a field is a
bundle that will be missing rows.

**What memory_get may hand over.** Retrieval holds content back from retired
memories (21.1), and a get that returned it anyway would be the way around
that rule rather than an exception to it. So memory_get answers with the same
asymmetry the layers use, keyed off what the entity currently points at.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from mashu import bootstrap, db, proposals, resolution, retrieval, store
from mashu.errors import DuplicateProposalError, MashuError
from mashu.models import MemoryType, ProposalOperation, VersionStatus

#: The identity the client was configured with. Set it in the MCP client's own
#: configuration, one entry per agent, so that the event log names which agent
#: proposed what.
ACTOR_ENV_VAR = "MASHU_AGENT"
DEFAULT_ACTOR = "agent"

_session_id: UUID | None = None


def actor() -> str:
    """Which agent this server is speaking for."""
    return os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


def session(cur: psycopg.Cursor) -> UUID:
    """The agent_session this process's proposals belong to, opened on demand.

    Opened lazily rather than at startup so that a process that never touches
    the store leaves no row, and opened here rather than in session_bootstrap
    so that an agent which skipped the bootstrap still has its proposals
    bundled. Whether the bootstrap happened is a separate question, and the
    event log is where it is answered.
    """
    global _session_id
    if _session_id is None:
        cur.execute(
            "INSERT INTO agent_session (agent) VALUES (%s) RETURNING session_id",
            (actor(),),
        )
        _session_id = cur.fetchone()["session_id"]
    return _session_id


# --------------------------------------------------------------------------
# shaping the answers
# --------------------------------------------------------------------------
def _plain(value: Any) -> Any:
    """JSON-safe form. UUIDs and timestamps become strings, nothing else moves."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _failure(error: MashuError, **extra: Any) -> dict[str, Any]:
    """An error the agent is meant to act on rather than merely report.

    The duplicate and resolution checks both stop the call in order to hand
    something back: what is already in the queue, or what already exists. That
    is data, so it comes back as data.
    """
    return _plain({"ok": False, "error": str(error), **extra})


def build_server() -> Any:
    """Construct the MCP server with the six tools of section 6."""
    from mcp.server import MCPServer

    server = MCPServer(
        name="mashu",
        instructions=(
            "Shared knowledge state. Call session_bootstrap once at the start "
            "of every session before anything else: it returns the index of "
            "what is known, and without it memory_search has nothing to aim "
            "at. Propose changes rather than assuming them; nothing here is "
            "edited in place."
        ),
    )

    @server.tool()
    def session_bootstrap(scopes: list[str] | None = None) -> dict[str, Any]:
        """The fixed context for a session start: preferences, current state, and
        the index of every scope. Call this once, first, before any search.

        scopes optionally narrows the current state to scopes already known to
        be in play; the index always covers the whole ledger.
        """
        with db.transaction() as cur:
            session(cur)
            got = bootstrap.session_bootstrap(
                cur,
                actor=actor(),
                scopes=[UUID(s) for s in scopes] if scopes else None,
            )
            return _plain(
                {
                    "ok": True,
                    "scope_index": got.scope_index,
                    "preferences": got.preferences,
                    "current_state": got.current_state,
                    "trimmed": got.trimmed,
                    "tokens": got.tokens,
                    "note": (
                        "Entries under trimmed had their content dropped to stay "
                        "inside the token ceiling; fetch them with memory_get."
                    ),
                }
            )

    @server.tool()
    def memory_search(
        query: str,
        scope: str | None = None,
        types: list[str] | None = None,
        limit: int = retrieval.DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Search the knowledge state. Answers in three layers.

        active: current knowledge, usable as it stands.
        unreviewed: written but not yet confirmed by a human. Usable, but do
            not make a definite claim on it alone, and do not propose the same
            thing again.
        retired: refuted, parked or turned down. Content is deliberately
            withheld; the reason is what you are given. Do not re-derive these,
            and if you argue against the reason, bring new grounds.
        """
        with db.transaction() as cur:
            got = retrieval.retrieve(
                cur,
                query,
                actor=actor(),
                scope_id=UUID(scope) if scope else None,
                types=[MemoryType(t) for t in types] if types else None,
                limit=limit,
            )
            return _plain(
                {
                    "ok": True,
                    "scopes_detected": got.scopes,
                    "active": got.active,
                    "unreviewed": got.unreviewed,
                    "retired": got.retired,
                    "dropped_unreviewed": got.dropped_unreviewed,
                }
            )

    @server.tool()
    def memory_get(memory_id: str) -> dict[str, Any]:
        """One memory by ID, with the same content rules search uses.

        A retired memory answers with its status and the reason it was retired,
        never with its content.
        """
        with db.transaction() as cur:
            try:
                return _plain(_read_one(cur, UUID(memory_id)))
            except MashuError as e:
                return _failure(e)

    @server.tool()
    def scope_list() -> dict[str, Any]:
        """Every active scope with its one-line summary."""
        with db.transaction() as cur:
            cur.execute(
                "SELECT scope_id, name, description FROM scope "
                "WHERE status = 'active' ORDER BY name"
            )
            return _plain({"ok": True, "scopes": cur.fetchall()})

    @server.tool()
    def entity_resolve(scope: str, title: str) -> dict[str, Any]:
        """Existing entities that look like this title, before proposing a new one.

        Section 20: adding a version to what already exists is nearly always
        better than creating a rival entity beside it.
        """
        with db.transaction() as cur:
            found = resolution.find_similar(cur, scope_id=UUID(scope), title=title)
            return _plain({"ok": True, "candidates": found})

    @server.tool()
    def memory_propose(
        operation: str,
        payload: dict[str, Any],
        memory_id: str | None = None,
        based_on_version: str | None = None,
        allow_duplicate: bool = False,
        allow_similar: bool = False,
    ) -> dict[str, Any]:
        """Propose a change. Nothing here edits the knowledge state directly.

        operation is one of create, update_version, change_status, restore or
        merge. For create, payload carries scope_id, type, title, content and
        source_type. For update_version, memory_id and based_on_version name
        what is being changed and payload carries content and source_type. For
        change_status, payload carries version_id, status and reason.

        The answer says which commit line the gate put this on: auto means it
        is already the current reading, candidate means it is readable but
        tagged unreviewed until a human confirms it, human_review means
        nothing changed until a human decides.

        A refusal is usually informative rather than final. Being told the same
        proposal already exists, or that a similar entity does, comes back with
        what was found; look at it, and pass allow_duplicate or allow_similar
        only if it really is different.
        """
        with db.transaction() as cur:
            try:
                result = proposals.propose(
                    cur,
                    actor=actor(),
                    operation=ProposalOperation(operation),
                    payload=payload,
                    target_memory=UUID(memory_id) if memory_id else None,
                    based_on_version=UUID(based_on_version) if based_on_version else None,
                    session_id=session(cur),
                    allow_duplicate=allow_duplicate,
                    allow_similar=allow_similar,
                )
            except DuplicateProposalError as e:
                return _failure(e, existing=e.existing)
            except resolution.SimilarEntityError as e:
                return _failure(e, candidates=e.candidates)
            except MashuError as e:
                return _failure(e)

            return _plain(
                {
                    "ok": True,
                    "proposal_id": result["proposal"]["proposal_id"],
                    "memory_id": result["proposal"]["target_memory"],
                    "version_id": result["proposal"]["applied_version"],
                    "commit_line": str(result["ruling"].decision),
                    "why": result["ruling"].reason,
                }
            )

    return server


def _read_one(cur: psycopg.Cursor, memory_id: UUID) -> dict[str, Any]:
    """One memory, showing only what its state allows (21.1)."""
    entity = store.get_entity(cur, memory_id)
    answer = {
        "ok": True,
        "memory_id": entity["memory_id"],
        "scope_id": entity["scope_id"],
        "type": entity["type"],
        "title": entity["title"],
        "entity_status": entity["status"],
    }

    if entity["active_version"] is not None:
        version = store.get_version(cur, entity["active_version"])
        return answer | {
            "layer": "active",
            "version_id": version["version_id"],
            "status": version["status"],
            "content": version["content"],
        }

    latest = store.get_version(cur, entity["latest_version"])
    if VersionStatus(latest["status"]) is VersionStatus.CANDIDATE:
        return answer | {
            "layer": "unreviewed",
            "version_id": latest["version_id"],
            "status": latest["status"],
            "content": latest["content"],
            "tag": retrieval.UNREVIEWED_TAG,
        }

    return answer | {
        "layer": "retired",
        "version_id": latest["version_id"],
        "status": latest["status"],
        "reason": latest["reason"],
        "note": "content withheld: this memory is retired (21.1)",
    }


def main() -> None:
    """Run the server over stdio, which is how the CLIs start it."""
    build_server().run()
