import math

import numpy as np

from smartrouter.analysis import holm, paired
from smartrouter.book import BUY, SELL
from smartrouter.metrics import summarize


class _V:
    def __init__(self, name):
        self.name = name


class _Ctx:
    def __init__(self):
        self.children = {}
        self.n_cancels = 0


class _Strat:
    name = "toy"
    done_t = None


class _Parent:
    def __init__(self, side):
        self.side = side
        self.qty = 300
        self.t0 = 0
        self.end_ns = 10
        self.arrival_mid = 10_000
        self.arrival_fair = 10_000.0


class _Market:
    def __init__(self, side, fills):
        self.parent = _Parent(side)
        self.ctx = _Ctx()
        self.rfills = fills
        self.seed = 1
        self.venues = [_V("A"), _V("B")]
        self.n_events = 0

    def fair_at(self, t):
        return 10_000.0

    def mid(self):
        return 10_000


def test_shortfall_includes_fees_and_signs():
    # buy 100 @ 10_050 taker fee 30, 200 @ 9_950 maker rebate -20
    fills = [(1, 0, BUY, 10_050, 100, "T", 30, 1), (2, 1, BUY, 9_950, 200, "M", -20, 2)]
    r = summarize(_Market(BUY, fills), _Strat())
    cost = 100 * (50 + 30) + 200 * (-50 - 20)
    assert math.isclose(r["is_bps"], cost / (300 * 10_000) * 1e4)
    assert math.isclose(r["fee_bps"], (100 * 30 - 200 * 20) / (300 * 10_000) * 1e4)
    assert r["maker_share"] == 200 / 300
    # a sell at a higher price is a gain
    fills = [(1, 0, SELL, 10_050, 300, "M", -20, 1)]
    r = summarize(_Market(SELL, fills), _Strat())
    assert math.isclose(r["is_bps"], 300 * (-50 - 20) / (300 * 10_000) * 1e4)


def _res(strategy, values):
    return [{"strategy": strategy, "seed": i, "is_bps": v} for i, v in enumerate(values)]


def test_paired_difference_statistics():
    rng = np.random.default_rng(0)
    base = rng.normal(5, 10, 200)
    better = base - 0.5 + rng.normal(0, 0.2, 200)
    pr = paired(_res("a", better) + _res("b", base), "a", "b")
    assert pr["n"] == 200
    assert abs(pr["mean"] + 0.5) < 0.05
    assert pr["ci_lo"] < pr["mean"] < pr["ci_hi"] and pr["ci_hi"] < 0
    assert pr["p"] < 1e-10 and pr["win_rate"] > 0.95


def test_holm_adjustment():
    adj = holm([0.01, 0.04, 0.03])
    assert adj == [0.03, 0.06, 0.06]
