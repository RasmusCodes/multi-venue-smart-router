"""Run one parent order through the simulated market."""

from __future__ import annotations

import time

from .config import NS_PER_S, Scenario
from .market import Market
from .metrics import summarize
from .router import RouterContext, make_strategy


def run_episode(scenario: Scenario, strategy: str, seed: int, stats=None, trace=False, parent=None,
                overrides=None, return_market=False):
    """Simulate warm-up, the parent order's horizon and a tail; return the metrics dict."""
    t0 = time.perf_counter()
    m = Market(scenario, seed)
    strat = make_strategy(strategy, stats=stats, trace=trace, **(overrides or {}))
    m.attach(strat, parent or scenario.parent, RouterContext)
    m.run(m.end_time())
    res = summarize(m, strat, wall_s=time.perf_counter() - t0) if not strategy.startswith("explore") else {}
    if return_market:
        return res, m, strat
    return res


def counterfactual_fair(scenario: Scenario, seed: int, times_ns):
    """Fair value at the given times in the same seed's market WITHOUT the parent order."""
    m = Market(scenario, seed)
    m.run(max(times_ns))
    return [m.fair_at(t) for t in times_ns]


def impact_bps(scenario: Scenario, seed: int, result: dict, market) -> float:
    """Permanent impact of the parent: fair value with minus without it, at the end of the horizon."""
    p = market.parent
    (f_without,) = counterfactual_fair(scenario, seed, [p.end_ns])
    return p.side * (market.fair_at(p.end_ns) - f_without) / p.arrival_mid * 1e4


__all__ = ["run_episode", "counterfactual_fair", "impact_bps", "NS_PER_S"]
