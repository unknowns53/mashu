from __future__ import annotations

import pytest

from mashu import projects, routing, scopes, task_actions
from mashu.errors import MashuError


def test_task_project_resolution_uses_the_only_project_in_the_routed_scope(cur, tmp_path):
    scope = scopes.create_scope(cur, name="project scope", actor="user")
    projects.create_project(cur, name="unscoped project", actor="user")
    routed = projects.create_project(
        cur, name="first project", scope_id=scope["scope_id"], actor="user"
    )
    routing.add_route(cur, path_prefix=str(tmp_path), scope_id=scope["scope_id"], actor="user")
    assert task_actions.resolve_task_project(cur, None, cwd=str(tmp_path)) == routed["project_id"]

    projects.create_project(cur, name="second project", scope_id=scope["scope_id"], actor="user")
    with pytest.raises(MashuError, match="choose one with --project"):
        task_actions.resolve_task_project(cur, None, cwd=str(tmp_path))
