from __future__ import annotations

import pytest

from mashu import projects, routing, task_actions
from mashu.errors import MashuError


def test_task_project_resolution_uses_the_project_this_directory_is_routed_to(cur, tmp_path):
    project = projects.create_project(cur, name="routed project", actor="user")
    projects.create_project(cur, name="another project", actor="user")
    routing.add_route(
        cur, path_prefix=str(tmp_path), project_id=project["project_id"], actor="user"
    )
    assert task_actions.resolve_task_project(cur, None, cwd=str(tmp_path)) == project["project_id"]

    with pytest.raises(MashuError, match="belongs to no project"):
        task_actions.resolve_task_project(cur, None, cwd=str(tmp_path.parent))
