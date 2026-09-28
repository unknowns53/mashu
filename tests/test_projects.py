from __future__ import annotations

import pytest

from conftest import expire
from mashu import projects, tasks
from mashu.errors import MashuError, RefusedError, UnknownProjectError


def test_a_project_name_is_taken_once_and_a_near_miss_is_answered_with_it(cur, scope_id):
    first = projects.create_project(cur, name="mashu", actor="user", scope_id=scope_id)
    assert first["scope_id"] == scope_id
    assert first["archived_at"] is None

    with pytest.raises(MashuError, match="already exists"):
        projects.create_project(cur, name="mashu", actor="user")
    with pytest.raises(UnknownProjectError, match="mashu"):
        projects.require_project(cur, "mashi")


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

    listed = projects.list_projects(cur)[0]
    assert listed["project_id"] == project["project_id"]
    assert (listed["n_active"], listed["n_dormant"], listed["n_closed"]) == (1, 1, 1)
    assert working["activity"] == "active"


def test_archiving_takes_a_project_off_the_list_once_and_leaves_its_tasks_alone(cur):
    projects.create_project(cur, name="mashu", actor="user")
    task = tasks.task_create(cur, project="mashu", name="implement the v3 schema", actor="agent")
    archived = projects.archive_project(cur, "mashu", actor="user")
    assert archived["archived_at"] is not None

    assert projects.list_projects(cur) == []
    assert len(projects.list_projects(cur, include_archived=True)) == 1

    with pytest.raises(MashuError, match="already archived"):
        projects.archive_project(cur, "mashu", actor="user")
    assert tasks.task_get(cur, task["task"]["task_id"])["activity"] == "active"
    assert projects.show_project(cur, "mashu")["n_active"] == 1
