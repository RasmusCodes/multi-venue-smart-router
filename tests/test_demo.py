"""The browser demo must reproduce the experiments seed for seed."""

import json

from smartrouter import demo
from smartrouter.config import ParentConfig, preset
from smartrouter.episode import run_episode
from smartrouter.market import Market
from smartrouter.router import RouterContext, make_strategy


def _digest_direct(scenario, strategy, seed, parent, overrides=None):
    m = Market(scenario, seed)
    m.attach(make_strategy(strategy, **(overrides or {})), parent, RouterContext)
    m.run(m.end_time())
    return m.digest()


def test_demo_trace_run_matches_a_plain_run():
    cfg = {"scenario": "baseline", "strategy": "sync", "seed": 3,
           "parent": {"side": 1, "qty": 4000, "horizon_s": 60, "slices": 4, "tail_s": 10}}
    out = json.loads(demo.run_one(json.dumps(cfg)))
    sc = preset("baseline")
    parent = ParentConfig(side=1, qty=4000, horizon_s=60, slices=4, tail_s=10)
    assert out["digest"] == _digest_direct(sc, "sync", 3, parent)
    assert out["metrics"]["filled"] == 4000
    assert out["samples"] and out["fills"] and out["children"]
    # stepping the clock in 500 ms increments must not change anything
    res = run_episode(sc, "sync", 3, parent=parent)
    assert abs(res["is_bps"] - out["metrics"]["is_bps"]) < 1e-12


def test_demo_metrics_and_info():
    cfg = {"scenario": "homogeneous", "strategies": ["nbbo", "post_primary"], "seeds": [1, 2],
           "parent": {"side": -1, "qty": 2000, "horizon_s": 30, "slices": 2, "tail_s": 5}}
    rows = json.loads(demo.run_metrics(json.dumps(cfg)))
    assert len(rows) == 4 and {r["strategy"] for r in rows} == {"nbbo", "post_primary"}
    info = json.loads(demo.scenario_info("baseline"))
    assert [v["name"] for v in info["venues"]] == ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO"]
