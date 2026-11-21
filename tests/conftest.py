import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import pytest  # noqa: E402

from smartrouter.config import ParentConfig, preset  # noqa: E402


@pytest.fixture
def short_parent():
    """A small, quick parent order for integration tests."""
    return ParentConfig(side=1, qty=4_000, horizon_s=60.0, slices=4, tail_s=10.0)


@pytest.fixture
def baseline_fast():
    sc = preset("baseline")
    sc.market.warmup_s = 20.0
    return sc
