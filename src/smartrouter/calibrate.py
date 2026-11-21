"""Fit the router's venue statistics on training seeds.

Strategies that use a model (``model``, ``post_queue``, ``ck``, ``smart``) read
only these statistics, fitted on seeds that are never used for evaluation:

* ``survival[v]``  - share of a synchronised IOC's requested (displayed)
                     quantity that actually fills at venue v;
* ``cleanup_fee``  - average taker fee per share paid when crossing the spread;
* ``post[v]``      - exploration samples (exec, cf, Q0, drift) per venue, from
                     single-venue posts (see ``router.explore``);
* ``joint``        - joint samples from simultaneous posts on every lit venue
                     (scenarios for the Cont-Kukanov allocator).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import ParentConfig, Scenario
from .parallel import pmap

TRAIN_SEED0 = 900_000


def calibrate(scenario: Scenario, n_single=120, n_joint=120, n_take=80, wait_s=11.25, workers=None, log=print):
    t0 = time.time()
    explore_parent = ParentConfig(side=1, qty=100, horizon_s=300.0, slices=1, tail_s=15.0)
    single = pmap("explore", [("explore", TRAIN_SEED0 + i) for i in range(n_single)], scenario,
                  parent=explore_parent, workers=workers)
    joint = pmap("explore", [("explore_joint", TRAIN_SEED0 + 10_000 + i) for i in range(n_joint)], scenario,
                 parent=explore_parent, workers=workers)
    post = {}
    for chunk in single:
        for (vi, ex, cf, q0, dm, hs_t, dmid) in chunk["single"]:
            post.setdefault(str(vi), []).append([ex, round(cf, 6), q0, dm, hs_t, dmid])
    joint_rows = []
    for chunk in joint:
        for row in chunk["joint"]:
            joint_rows.append({"v": {str(k): v for k, v in row["v"].items()}, "dm": row["dm"], "hsT": row["hsT"],
                               "dmid": row["dmid"]})
    # take survival from synchronised sweeps of large slices
    take_parent = ParentConfig(side=0, qty=20_000, horizon_s=300.0, slices=5, tail_s=5.0)
    kids = pmap("children", [("sync", TRAIN_SEED0 + 20_000 + i) for i in range(n_take)], scenario,
                parent=take_parent, workers=workers)
    req, got, fee_q, fee_sum = {}, {}, 0, 0
    for chunk in kids:
        for (kind, vi, qty, filled, seen, fees) in chunk:
            if kind != "take":
                continue
            req[vi] = req.get(vi, 0) + qty
            got[vi] = got.get(vi, 0) + filled
            fee_q += filled
            fee_sum += fees
    survival = {str(vi): round(got[vi] / req[vi], 4) for vi in sorted(req) if req[vi]}
    stats = {
        "scenario": scenario.name,
        "wait_s": wait_s,
        "train_seeds": {"single": [TRAIN_SEED0, TRAIN_SEED0 + n_single - 1],
                        "joint": [TRAIN_SEED0 + 10_000, TRAIN_SEED0 + 10_000 + n_joint - 1],
                        "take": [TRAIN_SEED0 + 20_000, TRAIN_SEED0 + 20_000 + n_take - 1]},
        "survival": survival,
        "cleanup_fee": round(fee_sum / fee_q, 3) if fee_q else 15.0,
        "post": post,
        "joint": joint_rows,
        "n_post": {k: len(v) for k, v in post.items()},
        "n_joint": len(joint_rows),
        "seconds": round(time.time() - t0, 1),
    }
    log(f"calibrated {scenario.name}: {sum(stats['n_post'].values())} single posts, {len(joint_rows)} joint, "
        f"survival {survival}, cleanup fee {stats['cleanup_fee']} ({stats['seconds']} s)")
    return stats


def save(stats, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, separators=(",", ":"))


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def summary(stats, scenario: Scenario) -> dict:
    """Human-readable digest of fitted statistics (for docs and the report)."""
    out = {"survival": {}, "post": {}, "cleanup_fee": stats["cleanup_fee"], "n_joint": stats["n_joint"]}
    for k, s in stats["survival"].items():
        out["survival"][scenario.venues[int(k)].name] = s
    for k, rows in stats["post"].items():
        n = len(rows)
        if not n:
            continue
        ex = sum(r[0] for r in rows) / n
        q0 = sum(r[2] for r in rows) / n
        cf = sum(r[1] for r in rows) / n
        dmid = sum(r[5] for r in rows) / n
        out["post"][scenario.venues[int(k)].name] = {"n": n, "mean_exec": round(ex), "mean_queue": round(q0),
                                                     "mean_cancel_frac": round(cf, 3),
                                                     "mean_drift_pu": round(dmid, 1)}
    return out
