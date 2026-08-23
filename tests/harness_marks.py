"""Which phase each unbuilt harness step is waiting on (specification 28).

The scenarios are written before the machinery, so a step that cannot run yet
carries the phase that will make it runnable. Listing them in one module keeps
the schedule in a single place rather than scattered through the scenarios.
"""

import pytest

skip_until_retrieval = pytest.mark.skip(
    reason="Retrieval Pipeline (specification 21) is Phase 2, weeks 9 to 10"
)
skip_until_entity_resolution = pytest.mark.skip(
    reason="Entity Resolution (specification 20) needs embeddings, weeks 9 to 10"
)
