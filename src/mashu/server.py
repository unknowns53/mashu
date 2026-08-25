"""The v2 MCP boundary: push the small, proven knowledge state to agents."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from uuid import UUID

from mashu import bootstrap, db, memories, routing, scopes, temporary, traces
from mashu.errors import MashuError

ACTOR_ENV_VAR = "MASHU_AGENT"
DEFAULT_ACTOR = "agent"


def actor() -> str:
    """Return the identity configured for this MCP process."""
    return os.environ.get(ACTOR_ENV_VAR) or DEFAULT_ACTOR


def _plain(value: Any) -> Any:
    """Turn database values into values accepted by the MCP JSON boundary."""
    if isinstance(value, (UUID, datetime, date)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _failure(error: MashuError) -> dict[str, Any]:
    return {"ok": False, "error": str(error)}


def _scope(cur: Any, name: str | None) -> tuple[UUID | None, str | None, bool]:
    """Resolve an explicit scope or the longest route for the current cwd."""
    if name is not None:
        row = scopes.require_scope(cur, name)
        return row["scope_id"], row["name"], False
    scope_id, routed = routing.resolve(cur, os.getcwd())
    if scope_id is None:
        return None, None, routed
    cur.execute("SELECT name FROM scope WHERE scope_id = %s", (scope_id,))
    row = cur.fetchone()
    return scope_id, row["name"] if row else None, routed


def build_server() -> Any:
    """Build the six-tool MCP server used by an agent session."""
    from mcp.server import MCPServer

    server = MCPServer(
        name="mashu",
        instructions=(
            "Knowledge is pushed, not searched. Call session_bootstrap once at "
            "the start of a session; traces are dated observations, not knowledge."
        ),
    )

    @server.tool()
    def session_bootstrap(scope: str | None = None) -> dict[str, Any]:
        """Receive the small set of active knowledge for this session.

        Knowledge is pushed, not searched: call this once at session start. A
        trace is an unverified observation, so trace_put what you derive and
        pain_report what actually hurt. Nothing an agent writes becomes
        knowledge without a human decision.
        """
        try:
            with db.transaction() as cur:
                scope_id, scope_name, routed = _scope(cur, scope)
                answer = bootstrap.session_bootstrap(
                    cur,
                    actor=actor(),
                    scope_id=scope_id,
                    scope_name=scope_name,
                    routed=routed,
                )
                answer = {
                    **answer,
                    "ok": True,
                    "note": (
                        "Knowledge is pushed, not searched. Call trace_put for a "
                        "later re-derivation and pain_report when forgetting caused pain; "
                        "nothing an agent writes becomes knowledge without a human decision."
                    ),
                }
                return _plain(answer)
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def pain_report(
        kind: str,
        what: str,
        prevention: str,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Record a real pain and show the evidence that may make it count.

        The ledger records what forgetting cost. A second friction can turn a
        trace into durable evidence, and an incident can create a nomination;
        neither path bypasses human admission. This is how forgetting gets
        counted, not a way to write knowledge directly.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                from mashu import ledger

                answer = ledger.report_pain(
                    cur,
                    kind=kind,
                    what=what,
                    prevention=prevention,
                    actor=actor(),
                    scope_id=scope_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def trace_put(content: str, scope: str | None = None) -> dict[str, Any]:
        """Leave a dated observation so a later re-derivation is provable.

        Traces are not knowledge and are never pushed to another session. They
        expire, but pain_report can freeze one into ledger evidence when the
        same work must be done again. A human still decides any admission.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                answer = traces.put_trace(
                    cur,
                    content=content,
                    actor=actor(),
                    scope_id=scope_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def trace_search(
        query: str | None = None,
        scope: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Search dated, unverified observations only.

        This is the sole search surface because v2 knowledge is pushed. The
        returned rows are observations from a date, not current knowledge;
        use pain_report to prove that repeating one mattered.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                found = traces.search_traces(
                    cur,
                    query=query,
                    scope_id=scope_id,
                    limit=limit,
                )
                rows = [{**row, "created_at": row.get("created_at")} for row in found]
                return _plain(
                    {
                        "ok": True,
                        "traces": rows,
                        "note": "These are dated, unverified observations, not knowledge.",
                    }
                )
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def memory_list(scope: str) -> dict[str, Any]:
        """List active knowledge for one named scope.

        Memory is intentionally read by push at bootstrap, not discovered by a
        general search. Human admission is required before anything appears in
        this list or reaches another session.
        """
        try:
            with db.transaction() as cur:
                row = scopes.require_scope(cur, scope)
                found = memories.active_memories(cur, scope_id=row["scope_id"])
                return _plain({"ok": True, "scope": row["name"], "memories": found})
        except MashuError as error:
            return _failure(error)

    @server.tool()
    def temporary_put(
        content: str,
        days: float,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Record a short-lived condition without pretending it is knowledge.

        Temporary context expires on its own and is not pushed as a memory.
        Longer-lived claims belong in pain_report or a human's remember path;
        an agent cannot make them active by writing them here.
        """
        try:
            with db.transaction() as cur:
                scope_id, _, _ = _scope(cur, scope)
                answer = temporary.put_temporary(
                    cur,
                    content=content,
                    actor=actor(),
                    days=days,
                    scope_id=scope_id,
                )
                return _plain({"ok": True, **answer})
        except MashuError as error:
            return _failure(error)

    return server


def main() -> None:
    """Run the MCP server over stdio."""
    build_server().run()
