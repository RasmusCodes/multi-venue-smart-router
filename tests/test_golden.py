"""Golden digests: lock bit-for-bit behaviour across platforms and Python builds.

The simulation path uses only IEEE-754 basic arithmetic and platform-stable
random streams, so these fingerprints must be identical on Windows, Linux,
macOS and in Pyodide (the browser demo). A deliberate behaviour change must
re-record them (run this file's ``__main__``) and say so in the commit.
"""

import pytest

from smartrouter.calibrate import calibrate
from smartrouter.config import ParentConfig, preset
from smartrouter.episode import run_episode

GOLDEN = {
    "nbbo": ("7e8a111607ca9920", 5738, 20),
    "sync": ("f7de79916c426c7e", 5732, 21),
    "post_primary": ("3be93cfdd100b030", 5819, 16),
    "smart": ("3a429ed41e6b7dc4", 5841, 15),
}


def _setup():
    sc = preset("baseline")
    sc.market.warmup_s = 20.0
    parent = ParentConfig(side=1, qty=4000, horizon_s=60.0, slices=4, tail_s=10.0)
    return sc, parent


def _digest(name, stats=None):
    sc, parent = _setup()
    _, m, _ = run_episode(sc, name, 42, stats=stats, parent=parent, return_market=True)
    return m.digest(), m.n_events, len(m.rfills)


@pytest.mark.parametrize("name", ["nbbo", "sync", "post_primary"])
def test_golden_digest(name):
    assert _digest(name) == GOLDEN[name]


def test_golden_digest_with_calibration():
    st = calibrate(preset("baseline"), n_single=3, n_joint=3, n_take=2, workers=1, log=lambda *a: None)
    assert _digest("smart", st) == GOLDEN["smart"]


if __name__ == "__main__":  # print fresh values after a deliberate behaviour change
    st = calibrate(preset("baseline"), n_single=3, n_joint=3, n_take=2, workers=1, log=lambda *a: None)
    for n in GOLDEN:
        print(n, _digest(n, st if n == "smart" else None))
