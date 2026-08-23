"""Shared setup for the harness scenarios (specification 28)."""

import pytest

from mashu import store
from mashu.models import MemoryType, SourceType


@pytest.fixture
def author(cur, scope_id):
    """A helper that writes one entity with its first version into the scope."""

    def _author(title, content, type=MemoryType.FACT, adopt=True):
        return store.create_entity(
            cur,
            scope_id=scope_id,
            type=type,
            title=title,
            content=content,
            source_type=SourceType.AGENT,
            created_by="claude",
            actor="claude",
            adopt=adopt,
        )

    return _author
