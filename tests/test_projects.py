from __future__ import annotations

import pytest

from conftest import expire
from mashu import db, scopes, tasks
from mashu.errors import MashuError, RefusedError


def test_a_scope_name_is_taken_once_and_a_near_miss_is_answered_with_it(cur):
    scopes.create_scope(cur, name="mashu", actor="user")

    with pytest.raises(MashuError, match="already exists"):
        scopes.create_scope(cur, name="mashu", actor="user")
    with pytest.raises(MashuError, match="mashu"):
        tasks.task_create(cur, project="mashi", name="measure compactions", actor="agent")


def test_a_project_id_from_project_list_finds_its_scope_through_mcp(mcp, dsn):
    with db.transaction(dsn) as cur:
        scope = scopes.create_scope(cur, name="mashu", actor="user")
    [listed] = mcp("project_list")["projects"]

    created = mcp("task_create", project=listed["project_id"], name="measure compactions")

    assert created["ok"] is True
    assert created["task"]["project_id"] == str(scope["scope_id"])


def test_a_banned_pattern_in_a_name_is_refused(cur):
    with pytest.raises(RefusedError):
        scopes.create_scope(cur, name="SECRETMARKER7 rewrite", actor="user")
    assert scopes.list_scopes(cur) == []


def test_a_listing_counts_what_each_scope_is_carrying(cur):
    scope = scopes.create_scope(cur, name="mashu", actor="user")
    working = tasks.task_create(cur, project="mashu", name="implement the v3 schema", actor="agent")
    quiet = tasks.task_create(cur, project="mashu", name="rewrite the poster", actor="agent")
    done = tasks.task_create(cur, project="mashu", name="calibrate the lease", actor="agent")

    expire(cur, quiet["task"]["task_id"])
    tasks.close(cur, done["task"]["task_id"], outcome="completed", actor="user")

    [listed] = scopes.list_scopes(cur)
    assert listed["scope_id"] == scope["scope_id"]
    counts = (listed["n_tasks_active"], listed["n_tasks_dormant"], listed["n_tasks_closed"])
    assert counts == (1, 1, 1)
    assert working["activity"] == "active"
