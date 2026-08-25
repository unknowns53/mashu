"""The cwd map (specification 8.2, `mashu route`)."""

from __future__ import annotations

from mashu import routing, scopes


def test_the_longest_prefix_wins(cur, scope_id):
    """A subtree can be split off without restating the tree around it."""
    inner = scopes.create_scope(cur, name="the inner tree", actor="user")["scope_id"]
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    routing.add_route(cur, path_prefix="/work/project/tools", scope_id=inner, actor="user")

    assert routing.resolve(cur, "/work/project/src") == (scope_id, True)
    assert routing.resolve(cur, "/work/project/tools/build") == (inner, True)


def test_a_route_to_nothing_is_an_answer(cur):
    """Deliberately uncaptured is a decision, and it has to be recordable.

    Without it the only reply to "this tree is not worth capturing" is the
    same reply an unmapped tree gets, and the prompt to write a route never
    stops arriving.
    """
    routing.add_route(cur, path_prefix="/work/scratch", scope_id=None, actor="user")
    assert routing.resolve(cur, "/work/scratch/today") == (None, True)


def test_an_unmapped_directory_says_so(cur, scope_id):
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    assert routing.resolve(cur, "/elsewhere") == (None, False)
    assert routing.resolve(cur, None) == (None, False)


def test_a_neighbour_with_a_longer_name_is_not_inside_the_route(cur, scope_id):
    """Matching runs on whole segments, not on characters."""
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="user")
    assert routing.resolve(cur, "/work/project-old") == (None, False)


def test_routing_a_directory_again_replaces_the_earlier_answer(cur, scope_id):
    """One directory cannot hold two contradictory answers at once."""
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
    """Reading created_by off the deleted row named the one person who did not.

    The log's job is to say who changed the map, and the author of a route is
    exactly the party a removal is evidence against.
    """
    routing.add_route(cur, path_prefix="/work/project", scope_id=scope_id, actor="the author")
    routing.remove_route(cur, path_prefix="/work/project", actor="somebody else")

    cur.execute("SELECT actor, detail FROM event_log WHERE event_type = 'route_removed'")
    row = cur.fetchone()
    assert row["actor"] == "somebody else"
    assert row["detail"]["path_prefix"] == "/work/project"


def test_one_spelling_per_directory(cur, scope_id):
    """Two spellings of one path would both look like the match."""
    routing.add_route(cur, path_prefix="/work/project/", scope_id=scope_id, actor="user")
    assert routing.all_routes(cur)[0]["path_prefix"] == "/work/project"
    assert routing.normalise("/work/project//") == "/work/project"
