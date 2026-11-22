"""Entry points for the browser demo (runs inside Pyodide, also plain CPython).

The demo runs this package unchanged, compiled to WebAssembly by Pyodide, so a
seed in the browser reproduces the same seed in the experiments bit for bit.

    run_one(json)      one strategy on one seed, with a trace for the charts
    run_metrics(json)  metrics only (for the Monte Carlo panel)
    scenario_info(name) venue table for the controls
"""

from __future__ import annotations

import json
import os
import time

from .book import BUY
from .config import NS_PER_S, ParentConfig, preset
from .market import Market
from .metrics import summarize
from .router import RouterContext, label, make_strategy

DATA_DIR = os.environ.get("SOR_DATA", os.path.join(os.path.dirname(__file__), "..", "smartrouter_data"))
_STATS = {}


def _stats(scenario):
    if scenario not in _STATS:
        path = os.path.join(DATA_DIR, "stats", f"{scenario}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                _STATS[scenario] = json.load(f)
        else:
            _STATS[scenario] = None
    return _STATS[scenario]


def _setup(cfg):
    sc = preset(cfg.get("scenario", "baseline"))
    p = {**sc.parent.to_dict(), **cfg.get("parent", {})}
    parent = ParentConfig.from_dict(p)
    return sc, parent


def _levels(book, side, n=3):
    return [[p, q] for (p, q) in book.levels(side, n)]


def run_one(cfg_json: str) -> str:
    cfg = json.loads(cfg_json)
    t_wall = time.perf_counter()
    sc, parent = _setup(cfg)
    seed = int(cfg.get("seed", 1))
    name = cfg["strategy"]
    stats = _stats(sc.name)
    strat = make_strategy(name, stats=stats, trace=True, **(cfg.get("overrides") or {}))
    m = Market(sc, seed)
    m.attach(strat, parent, RouterContext)
    t0 = m.start_time()
    end = m.end_time()
    m.run(t0)  # warm-up; the parent order starts at t0
    step = int(cfg.get("sample_ms", 500)) * 1_000_000
    samples = []
    t = t0
    while t < end:
        bb, ba = m.nbbo()
        row = [round((t - t0) / NS_PER_S, 3), bb, ba, round(m.F, 2)]
        books = []
        for v in m.lit:
            books.append([_levels(v.book, BUY), _levels(v.book, -1)])
        samples.append([row, books])
        t = min(end, t + step)
        m.run(t)
    res = summarize(m, strat)
    p = m.parent
    fills = [[round((tt - p.t0) / NS_PER_S, 4), vi, px, q, liq, fee] for (tt, vi, _s, px, q, liq, fee, _c) in m.rfills]
    kids = []
    for ch in m.ctx.children.values():
        kids.append([round((ch.sent - p.t0) / NS_PER_S, 4), ch.vi, ch.kind, ch.qty, ch.price, ch.filled,
                     ch.canceled, round((ch.arrive_est - p.t0) / NS_PER_S, 6), ch.snap_qty, ch.cancel_reason])
    notes = []
    for n in (strat.trace or []):
        n = dict(n)
        n["t"] = round((n["t"] - p.t0) / NS_PER_S, 4)
        if "L" in n:
            n["L"] = {str(k): v for k, v in n["L"].items()}
        notes.append(n)
    out = {
        "strategy": name, "label": label(name), "seed": seed, "scenario": sc.name,
        "side": p.side, "qty": p.qty, "horizon_s": p.horizon_ns / NS_PER_S, "slices": p.slices,
        "arrival_mid": p.arrival_mid, "metrics": res, "fills": fills, "children": kids, "notes": notes[:400],
        "samples": samples, "venues": [v.name for v in m.venues], "lit": [v.idx for v in m.lit],
        "wall_s": round(time.perf_counter() - t_wall, 3), "digest": m.digest(),
    }
    return json.dumps(out, separators=(",", ":"))


def run_metrics(cfg_json: str) -> str:
    """Metrics only, for many seeds: {"strategies": [...], "seeds": [...], ...}."""
    cfg = json.loads(cfg_json)
    sc, parent = _setup(cfg)
    stats = _stats(sc.name)
    out = []
    for seed in cfg["seeds"]:
        for name in cfg["strategies"]:
            m = Market(sc, int(seed))
            strat = make_strategy(name, stats=stats, **(cfg.get("overrides") or {}))
            m.attach(strat, parent, RouterContext)
            m.run(m.end_time())
            r = summarize(m, strat)
            out.append({k: r[k] for k in ("strategy", "seed", "is_bps", "price_bps", "fee_bps", "maker_share",
                                          "dark_fill", "fade_rate", "filled")})
    return json.dumps(out, separators=(",", ":"))


def scenario_info(name: str) -> str:
    sc = preset(name)
    rows = []
    for v in sc.venues:
        rows.append({"name": v.name, "kind": v.kind, "description": v.description, "make_fee": v.make_fee,
                     "take_fee": v.take_fee, "latency_us": v.latency_us, "bump_us": v.bump_us,
                     "protect": v.protect_us > 0})
    return json.dumps({"name": sc.name, "description": sc.description, "venues": rows,
                       "has_stats": _stats(name) is not None})
