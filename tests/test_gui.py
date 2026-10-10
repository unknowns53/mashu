from __future__ import annotations

import http.client
import threading
from collections.abc import Callable, Iterator

import pytest

from conftest import candidate, new_project, new_task, propose_change, remember
from mashu import application, db, gui, memories, task_history

MARKUP = "never trust a body that says <script>alert(1)</script> & means it"


@pytest.fixture
def fetch(dsn: str) -> Iterator[Callable[..., tuple[int, str]]]:
    """Ask a running GUI on a free loopback port, as a browser would."""
    server = gui.make_server(dsn, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def _fetch(path: str, method: str = "GET") -> tuple[int, str]:
        conn = http.client.HTTPConnection(*server.server_address, timeout=10)
        try:
            conn.request(method, path, body=b"" if method != "GET" else None)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    try:
        yield _fetch
    finally:
        server.shutdown()
        server.server_close()


def memory_count(dsn: str) -> int:
    with db.transaction(dsn) as cur:
        cur.execute("SELECT count(*) AS n FROM memory")
        return cur.fetchone()["n"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "APPROVE"])
def test_every_method_but_get_and_head_is_refused(fetch, method):
    assert fetch("/memories", method)[0] == 405
    assert fetch("/memories", "HEAD")[0] == 200


def test_a_write_reached_from_a_page_is_refused_by_the_database(dsn, fetch, monkeypatch):
    snapshot = application.status_snapshot

    def writing_snapshot(cur):
        memories.remember(cur, content="slipped in through a read", actor="user")
        return snapshot(cur)

    monkeypatch.setattr(application, "status_snapshot", writing_snapshot)

    assert fetch("/")[0] == 500
    assert memory_count(dsn) == 0


def test_stored_markup_is_shown_as_text_and_never_as_markup(dsn, fetch):
    with db.transaction(dsn) as cur:
        memory = remember(cur, MARKUP)

    for path in ("/memories", f"/memories/{memory['memory_id']}"):
        status, page = fetch(path)
        assert status == 200
        assert "<script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; means it" in page


def test_stored_rows_appear_in_full_on_their_pages_and_filters_narrow_them(
    dsn, fetch, legacy_bodies
):
    long_rule = "keep the release notes beside the tag they describe, " * 12
    with db.transaction(dsn) as cur:
        kept = remember(cur, long_rule.strip())
        memories.revise(cur, kept["memory_id"], content="keep notes by the tag", actor="user")
        timed = remember(cur, "spell out the timezone in every scheduled job")
        propose_change(
            cur, timed, "retire", retirement_kind="out_of_scope", retire_reason="cron moved to UTC"
        )
        new_project(cur)
        task = new_task(cur, "ship the browser view", goal="survey every list at once")
        task_history.checkpoint(
            cur,
            task["task"]["task_id"],
            actor="agent",
            what_changed="pages render",
            expect_updated_at=task["state"]["updated_at"],
            attempts=[{"attempt": "served it with a framework", "result": "too heavy"}],
        )
        candidate(cur, "check the vendored toolchain before building")

    page = fetch("/memories?q=TIMEZONE")[1]
    assert "spell out the timezone in every scheduled job" in page
    assert "keep notes by the tag" not in page

    detail = fetch(f"/memories/{kept['memory_id']}")[1]
    assert long_rule.strip() in detail and "keep notes by the tag" in detail

    assert "survey every list at once" in fetch("/tasks?project=enrai")[1]
    task_page = fetch(f"/tasks/{task['task']['task_id']}")[1]
    assert "pages render" in task_page and "served it with a framework" in task_page

    review = fetch("/review")[1]
    assert "check the vendored toolchain before building" in review
    assert "cron moved to UTC" in review
    assert fetch(f"/tasks/{kept['memory_id']}")[0] == 404
