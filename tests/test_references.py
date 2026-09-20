from __future__ import annotations

from mashu import cli, memories, memory_ui
from mashu.errors import MashuError


def test_cli_and_memory_screen_share_uuid_prefix_resolution(cur):
    row = memories.remember(
        cur,
        content="resolver parity uses the exact same short reference",
        actor="user",
    )
    prefix = str(row["memory_id"])[:8]

    assert cli._memory_ref(cur, prefix) == memory_ui._resolve_id(
        cur,
        prefix,
        table="memory",
        column="memory_id",
        label="memory",
    )


def test_cli_and_memory_screen_show_the_same_prefix_error(cur):
    messages = []
    for resolve in (
        lambda: cli._memory_ref(cur, "ffffffffffff"),
        lambda: memory_ui._resolve_id(
            cur,
            "ffffffffffff",
            table="memory",
            column="memory_id",
            label="memory",
        ),
    ):
        try:
            resolve()
        except MashuError as error:
            messages.append(str(error))

    assert messages[0] == messages[1]
    assert "check the ID or try another prefix" in messages[0]
