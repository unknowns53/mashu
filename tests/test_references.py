from __future__ import annotations

import pytest

from conftest import remember
from mashu import cli, memory_ui
from mashu.errors import MashuError


def test_cli_and_memory_screen_resolve_a_prefix_and_refuse_one_alike(cur):
    def on_screen(prefix):
        return memory_ui._resolve_id(
            cur, prefix, table="memory", column="memory_id", label="memory"
        )

    prefix = str(remember(cur, "resolver parity uses the same short reference")["memory_id"])[:8]
    assert cli._memory_ref(cur, prefix) == on_screen(prefix)

    with pytest.raises(MashuError) as from_cli:
        cli._memory_ref(cur, "ffffffffffff")
    with pytest.raises(MashuError) as from_screen:
        on_screen("ffffffffffff")
    assert str(from_cli.value) == str(from_screen.value)
    assert "check the ID or try another prefix" in str(from_cli.value)
