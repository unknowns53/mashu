
from __future__ import annotations

import os

import pytest

from mashu import routing, scopes


def test_the_longest_prefix_wins(cur, scope_id):
    inner = scopes.create_scope(cur, name="the inner tree", actor="user")["scope_id"]
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    routing.add_route(cur, path_prefix="/work/project/tools", scope_id=inner, actor="user")

    assert routing.resolve(cur, "/work/project/src") == (scope_id, True)
    assert routing.resolve(cur, "/work/project/tools/build") == (inner, True)


def test_a_route_to_nothing_is_an_answer(cur):
    routing.add_route(cur, path_prefix="/work/scratch", scope_id=None, actor="user")
    assert routing.resolve(cur, "/work/scratch/today") == (None, True)


def test_an_unmapped_directory_says_so(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    assert routing.resolve(cur, "/elsewhere") == (None, False)
    assert routing.resolve(cur, None) == (None, False)


def test_a_neighbour_with_a_longer_name_is_not_inside_the_route(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    assert routing.resolve(cur, "/work/project-old") == (None, False)


def test_routing_a_directory_again_replaces_the_earlier_answer(cur, scope_id):
    other = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    routing.add_route(cur, path_prefix="/work/project", scope_id=other, actor="user")

    assert len(routing.all_routes(cur)) == 1
    assert routing.resolve(cur, "/work/project") == (other, True)


def test_a_removed_route_leaves_the_directory_unmapped(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    assert routing.remove_route(cur, path_prefix="/work/project", actor="user") is True
    assert routing.remove_route(cur, path_prefix="/work/project", actor="user") is False
    assert routing.resolve(cur, "/work/project") == (None, False)


def test_the_removal_is_filed_under_whoever_removed_it(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="the author")
    routing.remove_route(cur, path_prefix="/work/project", actor="somebody else")

    cur.execute("SELECT actor, detail FROM event_log WHERE event_type = 'route_removed'")
    row = cur.fetchone()
    assert row["actor"] == "somebody else"
    assert row["detail"]["path_prefix"] == "/work/project"


def test_one_spelling_per_directory(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project/", scope_id=scope_id, actor="user")
    assert routing.all_routes(cur)[0]["path_prefix"] == "/work/project"
    assert routing.normalise("/work/project//") == "/work/project"


@pytest.mark.skipif(os.name != "nt", reason="the spellings being reconciled are Windows ones")
def test_a_windows_route_covers_the_tree_below_it(cur, scope_id):
    routing.add_route(cur, path_prefix="C:/Work/Project", scope_id=scope_id, actor="user")

    assert routing.resolve(cur, "C:\\Work\\Project") == (scope_id, True)
    assert routing.resolve(cur, "C:\\Work\\Project\\md\\run-01") == (scope_id, True)
    assert routing.resolve(cur, "c:/work/project/dft") == (scope_id, True)
    assert routing.resolve(cur, "C:\\Work\\Project-old") == (None, False)
