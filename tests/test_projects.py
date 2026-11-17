from __future__ import annotations

import pytest

from conftest import expire
from mashu import db, projects, tasks
from mashu.errors import MashuError, RefusedError


def test_a_project_name_is_taken_once_and_a_near_miss_is_answered_with_it(cur):
    projects.create_project(cur, name="mashu", actor="user")

    with pytest.raises(MashuError, match="already exists"):
        projects.create_project(cur, name="mashu", actor="user")
    with pytest.raises(MashuError, match="mashu"):
        tasks.task_create(cur, project="mashi", name="measure compactions", actor="agent")


def test_a_project_id_from_project_list_finds_its_project_through_mcp(mcp, dsn):
    with db.transaction(dsn) as cur:
        project = projects.create_project(cur, name="mashu", actor="user")
    [listed] = mcp("project_list")["projects"]

    created = mcp("task_create", project=listed["project_id"], name="measure compactions")

    assert created["ok"] is True
    assert created["task"]["project_id"] == str(project["project_id"])


def test_a_banned_pattern_in_a_name_is_refused(cur):
    with pytest.raises(RefusedError):
        projects.create_project(cur, name="SECRETMARKER7 rewrite", actor="user")
    assert projects.list_projects(cur) == []


def test_a_listing_counts_what_each_project_is_carrying(cur):
    project = projects.create_project(cur, name="mashu", actor="user")
    working = tasks.task_create(cur, project="mashu", name="implement the v3 schema", actor="agent")
    quiet = tasks.task_create(cur, project="mashu", name="rewrite the poster", actor="agent")
    done = tasks.task_create(cur, project="mashu", name="calibrate the lease", actor="agent")

    expire(cur, quiet["task"]["task_id"])
    tasks.close(cur, done["task"]["task_id"], outcome="completed", actor="user")

    [listed] = projects.list_projects(cur)
    assert listed["project_id"] == project["project_id"]
    counts = (listed["n_tasks_active"], listed["n_tasks_dormant"], listed["n_tasks_closed"])
    assert counts == (1, 1, 1)
    assert working["activity"] == "active"
