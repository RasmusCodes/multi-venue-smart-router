"""Run many episodes across processes (stdlib multiprocessing, spawn-safe on Windows)."""

from __future__ import annotations

import os
from multiprocessing import get_context

from .config import ParentConfig, Scenario

_G = {}


def _init(scenario_dict, stats, parent_dict):
    _G["scenario"] = Scenario.from_dict(scenario_dict)
    _G["stats"] = stats
    _G["parent"] = ParentConfig.from_dict(parent_dict) if parent_dict else None


def _run(task):
    from .episode import run_episode  # imported in the worker

    strategy, seed, with_impact, overrides = task
    sc = _G["scenario"]
    if with_impact:
        from .episode import impact_bps

        res, m, _ = run_episode(sc, strategy, seed, stats=_G["stats"], parent=_G["parent"], overrides=overrides,
                                return_market=True)
        res["impact_bps"] = impact_bps(sc, seed, res, m)
        return res
    return run_episode(sc, strategy, seed, stats=_G["stats"], parent=_G["parent"], overrides=overrides)


def _run_explore(task):
    from .episode import run_episode

    strategy, seed = task
    _, m, strat = run_episode(_G["scenario"], strategy, seed, parent=_G["parent"], return_market=True)
    return {"single": strat.rows_single, "joint": strat.rows_joint}


def _run_cf(task):
    """Fair value at ``end_ns`` in the same seed's market without the parent order."""
    from .episode import counterfactual_fair

    seed, end_ns = task
    (f,) = counterfactual_fair(_G["scenario"], seed, [end_ns])
    return seed, f


def _run_children(task):
    """Return per-child records (for fitting take survival / dark fill rates)."""
    from .episode import run_episode

    strategy, seed = task
    _, m, strat = run_episode(_G["scenario"], strategy, seed, stats=_G["stats"], parent=_G["parent"],
                              return_market=True)
    out = []
    for ch in m.ctx.children.values():
        out.append((ch.kind, ch.vi, ch.qty, ch.filled, ch.snap_qty, ch.fees))
    return out


def default_workers() -> int:
    env = os.environ.get("SOR_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, min(6, (os.cpu_count() or 2) - 2))


def pmap(fn_name, tasks, scenario: Scenario, stats=None, parent: ParentConfig | None = None, workers=None,
         progress=None):
    fn = {"run": _run, "explore": _run_explore, "children": _run_children, "cf": _run_cf}[fn_name]
    workers = workers or default_workers()
    args = (scenario.to_dict(), stats, parent.to_dict() if parent else None)
    if workers == 1:
        _init(*args)
        out = []
        for i, t in enumerate(tasks):
            out.append(fn(t))
            if progress:
                progress(i + 1, len(tasks))
        return out
    ctx = get_context("spawn")
    out = []
    with ctx.Pool(workers, initializer=_init, initargs=args) as pool:
        for i, r in enumerate(pool.imap(fn, tasks, chunksize=2)):
            out.append(r)
            if progress:
                progress(i + 1, len(tasks))
    return out
