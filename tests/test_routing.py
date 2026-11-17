from __future__ import annotations

import os

import pytest

from conftest import new_task, remember, retire
from mashu import routing, scopes
from mashu.errors import MashuError


def test_the_longest_prefix_wins(cur, scope_id):
    inner = scopes.create_scope(cur, name="the inner tree", actor="user")["scope_id"]
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    routing.add_route(cur, path_prefix="/work/project/tools", scope_id=inner, actor="user")

    assert routing.resolve(cur, "/work/project/src") == (scope_id, True)
    assert routing.resolve(cur, "/work/project/tools/build") == (inner, True)


def test_a_route_to_nothing_is_an_answer(cur):
    routing.add_route(cur, path_prefix="/work/scratch", scope_id=None, actor="user")
    assert routing.resolve(cur, "/work/scratch/today") == (None, True)


@pytest.mark.parametrize("cwd", ["/elsewhere", None, "/work/project-old"])
def test_an_unmapped_directory_says_so_even_beside_a_route(cur, scope_id, cwd):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    assert routing.resolve(cur, cwd) == (None, False)


def test_routing_a_directory_again_replaces_the_earlier_answer(cur, scope_id):
    other = scopes.create_scope(cur, name="somewhere else", actor="user")["scope_id"]
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    routing.add_route(cur, path_prefix="/work/project", scope_id=other, actor="user")

    assert len(routing.all_routes(cur)) == 1
    assert routing.resolve(cur, "/work/project") == (other, True)


def test_a_removed_route_leaves_the_directory_unmapped_and_names_who_removed_it(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="the author")
    assert routing.remove_route(cur, path_prefix="/work/project", actor="somebody else") is True
    assert routing.remove_route(cur, path_prefix="/work/project", actor="user") is False
    assert routing.resolve(cur, "/work/project") == (None, False)

    cur.execute("SELECT actor, detail FROM event_log WHERE event_type = 'route_removed'")
    got = [(row["actor"], row["detail"]["path_prefix"]) for row in cur.fetchall()]
    assert got == [("somebody else", "/work/project")]


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


@pytest.mark.parametrize(
    ("use", "outcome"),
    [
        (None, "deleted"),
        ("route", "refused"),
        ("active memory", "refused"),
        ("other scope of an active memory", "refused"),
        ("retired memory", "archived"),
        ("open task", "refused"),
    ],
)
def test_a_scope_is_removed_only_once_nothing_delivers_through_it(cur, scope_id, use, outcome):
    target = scopes.create_scope(cur, name="removable", actor="user")["scope_id"]
    if use == "route":
        routing.add_route(cur, path_prefix="/work/removable", scope_id=target, actor="user")
    elif use == "active memory":
        remember(cur, "a rule kept for the removable scope", scope_id=target, delivery="scope")
    elif use == "other scope of an active memory":
        remember(
            cur,
            "a rule kept for both scopes",
            scope_id=scope_id,
            delivery="scope",
            also_scope_ids=[target],
        )
    elif use == "retired memory":
        rule = remember(cur, "a rule once kept here", scope_id=target, delivery="scope")
        retire(cur, rule)
    elif use == "open task":
        new_task(cur, "a task filed here", target)

    if outcome == "refused":
        with pytest.raises(MashuError, match="still has"):
            scopes.remove_scope(cur, target, actor="user")
        return
    assert scopes.remove_scope(cur, target, actor="user")["removed"] == outcome
    assert "removable" not in [row["name"] for row in scopes.list_scopes(cur)]
    if outcome == "archived":
        with pytest.raises(MashuError, match="archived"):
            scopes.require_scope(cur, "removable")
