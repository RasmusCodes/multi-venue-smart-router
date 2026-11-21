"""Experiment definitions and runner.

Every experiment runs each strategy on the same seeds (common random
numbers): within a seed, all strategies face the same news, the same
background order arrivals and the same liquidity-provider decisions until
their own orders change the state. Comparisons are paired by seed.

Seeds: evaluation seeds start at 1; development seeds at 500,000; training
seeds (calibration) at 900,000. Nothing is tuned on evaluation seeds.
"""

from __future__ import annotations

import gzip
import json
import platform
import sys
import time
from pathlib import Path

from . import __version__
from .calibrate import calibrate, load, save
from .config import ParentConfig, preset
from .parallel import pmap

EVAL_SEED0 = 1
DEV_SEED0 = 500_000
WAIT_S = 11.25  # every passive wait is 11.25 s, so one calibration per scenario serves all runs

PATIENT = {"qty": 20_000, "horizon_s": 300.0, "slices": 20}
URGENT = {"qty": 20_000, "horizon_s": 300.0, "slices": 5}
URGENT_OVERRIDES = {"wait_frac": WAIT_S / 60.0}

EXPERIMENTS = {
    "E1": {
        "title": "Taking liquidity: routing a marketable slice",
        "question": "With every slice crossing the spread at once (five 4,000-share slices, larger than any "
                    "single venue's displayed queue), how much do fee awareness, synchronised arrival and a "
                    "fill model save against the naive NBBO sweep?",
        "runs": [{"label": "baseline", "scenario": "baseline", "parent": URGENT}],
        "strategies": ["primary", "nbbo", "fee", "sync", "model"],
        "baselines": ["nbbo"],
        "seeds": 300,
    },
    "E2": {
        "title": "Resting liquidity: where to post",
        "question": "Posting each 1,000-share slice at the NBBO for 11.25 s and crossing for the rest: does the "
                    "venue choice matter once fill probability, adverse selection and clean-up cost are counted?",
        "runs": [{"label": "baseline", "scenario": "baseline", "parent": PATIENT}],
        "strategies": ["sync", "post_primary", "post_rebate", "post_queue", "post_adaptive"],
        "baselines": ["post_primary"],
        "seeds": 300,
    },
    "E3": {
        "title": "Splitting a slice across venues",
        "question": "Does the Cont-Kukanov split between a market order and limit orders on several venues beat "
                    "the best single-venue post and pure taking?",
        "runs": [{"label": "baseline", "scenario": "baseline", "parent": PATIENT}],
        "strategies": ["model", "post_queue", "ck", "smart"],
        "baselines": ["post_queue"],
        "seeds": 300,
    },
    "E4": {
        "title": "Headline: the smart router against naive routers",
        "question": "End to end, does the full smart router beat a naive taking router and a naive posting router, "
                    "for a patient and an urgent schedule?",
        "runs": [
            {"label": "patient", "scenario": "baseline", "parent": PATIENT},
            {"label": "urgent", "scenario": "baseline", "parent": URGENT, "overrides": URGENT_OVERRIDES},
        ],
        "strategies": ["nbbo", "post_primary", "smart"],
        "baselines": ["nbbo", "post_primary"],
        "seeds": 300,
        "impact": True,
    },
    "E5": {
        "title": "Robustness across market regimes",
        "question": "Does the smart router's advantage survive lower fee caps, faster fading, higher volatility "
                    "and wider latency gaps (each regime with its own calibration)?",
        "runs": [{"label": s, "scenario": s, "parent": PATIENT}
                 for s in ("fee_cap_10", "fast_fade", "high_vol", "wide_latency")],
        "strategies": ["nbbo", "post_primary", "smart"],
        "baselines": ["nbbo", "post_primary"],
        "seeds": 200,
    },
    "E6": {
        "title": "Negative control: identical venues",
        "question": "With four identical venues and no dark pool, there is little to route around. Do the "
                    "smart router's gains shrink towards zero, as they should?",
        "runs": [{"label": "homogeneous", "scenario": "homogeneous", "parent": PATIENT}],
        "strategies": ["nbbo", "sync", "post_primary", "post_queue", "smart"],
        "baselines": ["nbbo", "post_primary"],
        "seeds": 200,
    },
}


def stats_for(scenario_name, stats_dir: Path, workers=None, log=print, sizes=(120, 120, 80)):
    path = stats_dir / f"{scenario_name}.json"
    if path.exists():
        return load(path)
    log(f"calibrating {scenario_name} on training seeds ...")
    st = calibrate(preset(scenario_name), n_single=sizes[0], n_joint=sizes[1], n_take=sizes[2], wait_s=WAIT_S,
                   workers=workers, log=log)
    save(st, path)
    return st


def run_experiment(key, out_dir, stats_dir, seeds=None, seed0=EVAL_SEED0, workers=None, log=print,
                   calib_sizes=(120, 120, 80)):
    spec = EXPERIMENTS[key]
    n = seeds or spec["seeds"]
    seed_list = list(range(seed0, seed0 + n))
    out = Path(out_dir) / key
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "experiment": key, "title": spec["title"], "version": __version__, "python": sys.version.split()[0],
        "platform": platform.platform(), "seeds": [seed_list[0], seed_list[-1]], "runs": [],
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for run in spec["runs"]:
        t0 = time.time()
        sc = preset(run["scenario"])
        parent = ParentConfig(**{**sc.parent.to_dict(), **run["parent"]})
        stats = stats_for(run["scenario"], Path(stats_dir), workers=workers, log=log, sizes=calib_sizes)
        overrides = run.get("overrides")
        tasks = [(s, seed, False, overrides) for seed in seed_list for s in spec["strategies"]]
        done = [0]

        def prog(i, total):
            if i == total or i % max(1, total // 10) == 0:
                log(f"  {key}/{run['label']}: {i}/{total} episodes ({time.time() - t0:.0f} s)")

        results = pmap("run", tasks, sc, stats=stats, parent=parent, workers=workers, progress=prog)
        if spec.get("impact"):
            ends = {}
            for r in results:
                ends[r["seed"]] = r["end_ns"]
            cf = dict(pmap("cf", sorted(ends.items()), sc, parent=parent, workers=workers))
            for r in results:
                f0 = cf[r["seed"]]
                r["impact_bps"] = r["side"] * (r["fair_end"] - f0) / r["arrival_mid"] * 1e4
        path = out / f"{run['label']}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        manifest["runs"].append({"label": run["label"], "scenario": run["scenario"], "parent": parent.to_dict(),
                                 "overrides": overrides, "episodes": len(results),
                                 "seconds": round(time.time() - t0, 1)})
        log(f"  wrote {path} ({len(results)} episodes, {time.time() - t0:.0f} s)")
    manifest["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(out / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def load_results(out_dir, key, label):
    path = Path(out_dir) / key / f"{label}.jsonl.gz"
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    with open(Path(out_dir) / key / f"{label}.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
