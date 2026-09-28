from __future__ import annotations

import asyncio
import io
import os
import pathlib
import shutil
from collections.abc import Callable, Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from mashu import db, ledger, memories, memory_changes, nominations, projects, screen, server, tasks
from mashu.migrate import migrate, migration_files

TEST_DB = os.environ.get("MASHU_TEST_DB", "mashu_test")
ADMIN_DSN = os.environ.get("MASHU_ADMIN_DSN", "dbname=postgres")


def fresh_database(name: str, template: str | None = None) -> str:
    copy = f' TEMPLATE "{template}"' if template else ""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"{copy}')
    return f"dbname={name}"


@pytest.fixture(scope="session")
def test_dsn() -> str:
    dsn = fresh_database(TEST_DB)
    applied = migrate(dsn)
    assert applied, "no migrations were applied to the test database"
    return dsn


@pytest.fixture(scope="session")
def committing_dsn() -> str:
    """A second database for tests that have to commit."""
    dsn = fresh_database(f"{TEST_DB}_committing")
    migrate(dsn)
    return dsn


@pytest.fixture(scope="session")
def migrated_template() -> str:
    name = f"{TEST_DB}_template"
    migrate(fresh_database(name))
    return name


@pytest.fixture
def dsn(migrated_template: str) -> str:
    """A committed database of the test's own, empty but for the migrations."""
    return fresh_database(f"{TEST_DB}_own", template=migrated_template)


@pytest.fixture
def old_store(tmp_path: pathlib.Path) -> Iterator[Callable[[str], str]]:
    """Build a database migrated only up to, not including, the named migration."""
    name = f"{TEST_DB}_old"

    def build(before: str) -> str:
        old = tmp_path / "old-migrations"
        old.mkdir()
        for path in migration_files():
            if path.name < before:
                shutil.copy(path, old / path.name)
        dsn = fresh_database(name)
        migrate(dsn, old)
        return dsn

    yield build
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def mcp(dsn: str, monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict]:
    """Call one MCP tool, as a client would, against the test's own committed database."""
    pytest.importorskip("mcp.server")
    monkeypatch.setenv("MASHU_DATABASE_URL", dsn)
    tools = server.build_server()
    return lambda tool, **arguments: (
        asyncio.run(tools.call_tool(tool, arguments)).structured_content
    )


@pytest.fixture
def cur(test_dsn: str) -> Iterator[psycopg.Cursor]:
    """A cursor whose transaction is rolled back when the test ends."""
    conn = psycopg.connect(test_dsn, row_factory=dict_row)
    try:
        with conn.cursor() as cursor:
            yield cursor
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def scope_id(cur: psycopg.Cursor):
    """A scope to hang test rows on."""
    from mashu.scopes import create_scope

    return create_scope(cur, name="test scope", actor="test")["scope_id"]


@pytest.fixture(autouse=True)
def banned_patterns(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the gate at a pattern nobody's real identity matches."""
    path = tmp_path / "banned-patterns"
    path.write_text("SECRETMARKER\\d+\n", encoding="utf-8")
    monkeypatch.setenv("MASHU_BANNED_PATTERNS", str(path))


@pytest.fixture
def known_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal the paging can be reasoned about, with no editor to launch."""
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("LINES", "40")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)


def keys(monkeypatch: pytest.MonkeyPatch, *typed: str) -> None:
    """Hand a line-read screen its keystrokes: one line each, in the order pressed."""
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in typed)))


def read_through(monkeypatch: pytest.MonkeyPatch, text: str, under: str, *after: str) -> None:
    """Open an item and press space until its last page shows, then press the keys after."""
    offset, spaces = 0, []
    while offset := screen.paged(text, offset, under)[1]:
        spaces.append("space")
    keys(monkeypatch, "", *spaces, *after)


def getkeys(monkeypatch: pytest.MonkeyPatch, *pressed: str) -> None:
    """Hand a raw-key screen its keys, one per read."""
    values = iter(pressed)
    monkeypatch.setattr(screen, "getkey", lambda: next(values))


def remember(cur: psycopg.Cursor, content: str, **kwargs):
    return memories.remember(cur, content=content, actor="user", **kwargs)


def retire(cur: psycopg.Cursor, memory, reason: str = "withdrawn", kind: str = "invalidated", **kw):
    return memories.retire(
        cur,
        memory["memory_id"],
        reason=reason,
        retirement_kind=kind,
        actor="user",
        _legacy_compat=kind == "legacy",
        **kw,
    )


def candidate(cur: psycopg.Cursor, content: str, *, kind: str = "rederivation", **kwargs):
    """A pending candidate standing on one recorded friction."""
    pain = ledger.report_pain(
        cur, kind="friction", what="looked it up again", prevention=content, actor="agent"
    )
    return nominations.create_nomination(
        cur, content=content, kind=kind, evidence=[pain["ledger_id"]], actor="agent", **kwargs
    )


def propose_change(cur: psycopg.Cursor, memory, operation: str, *, ledger_id=None, **kwargs):
    """A proposal against the Memory as it reads now, citing a ledger row it rests on."""
    detail = memories.memory_details(cur, memory["memory_id"])
    cited = ledger_id or memory["evidence"][0]
    kwargs.setdefault(
        "evidence", [{"kind": "ledger", "id": str(cited), "observation": "the rule moved on"}]
    )
    return memory_changes.propose(
        cur,
        target_memory_id=memory["memory_id"],
        target_revision_id=detail["current_revision_id"],
        target_updated_at=detail["updated_at"],
        operation=operation,
        actor="agent",
        **kwargs,
    )


def apply_change(cur, change, instruction="Apply it", *, version=None, request_id=None, **approval):
    """Apply a proposal on a quoted user instruction, at the version it was read at."""
    return memory_changes.apply(
        cur,
        change["change_id"],
        version=change["version"] if version is None else version,
        request_id=request_id or uuid4(),
        approval={
            "kind": "user_instruction",
            "instruction": instruction,
            "conversation_ref": "conversation:turn-42",
            **approval,
        },
        actor="agent",
    )


def new_project(cur: psycopg.Cursor, name: str = "enrai", **kwargs):
    return projects.create_project(cur, name=name, actor="user", **kwargs)


def new_task(cur: psycopg.Cursor, name: str, project: str = "enrai", **kwargs):
    return tasks.task_create(cur, project=project, name=name, actor="agent", **kwargs)


def update_task(cur: psycopg.Cursor, task_id, *, at=None, actor: str = "agent", **fields):
    """Replace a task's state, written against the state it has now unless told otherwise."""
    if at is None:
        at = tasks.task_get(cur, task_id)["state"]["updated_at"]
    return tasks.task_update(cur, task_id, actor=actor, expect_updated_at=at, **fields)


def expire(cur: psycopg.Cursor, task_id) -> None:
    """Age a task past its lease, which is all dormancy is."""
    cur.execute(
        "UPDATE task SET active_until = now() - interval '1 day', "
        "last_activity_at = now() - interval '15 days' WHERE task_id = %s",
        (task_id,),
    )


def committed_task(dsn: str, name: str, *, propose: str | None = None, reason: str = "", **kw):
    """One open task in project enrai, committed, optionally with a proposal standing on it."""
    with db.transaction(dsn) as cur:
        task_id = new_task(cur, name, goal=f"finish {name}", force=True, **kw)["task"]["task_id"]
        if propose:
            tasks.propose_close(cur, task_id, outcome=propose, reason=reason, actor="agent")
        return task_id


def task_row(dsn: str, task_id):
    with db.transaction(dsn) as cur:
        return tasks.task_get(cur, task_id)
