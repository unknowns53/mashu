from __future__ import annotations

import pytest

from mashu import routing, scopes, task_actions
from mashu.errors import MashuError


def test_task_project_resolution_uses_the_scope_this_directory_is_routed_to(cur, tmp_path):
    scope = scopes.create_scope(cur, name="project scope", actor="user")
    scopes.create_scope(cur, name="another scope", actor="user")
    routing.add_route(cur, path_prefix=str(tmp_path), scope_id=scope["scope_id"], actor="user")
    assert task_actions.resolve_task_project(cur, None, cwd=str(tmp_path)) == scope["scope_id"]

    with pytest.raises(MashuError, match="belongs to no scope"):
        task_actions.resolve_task_project(cur, None, cwd=str(tmp_path.parent))
