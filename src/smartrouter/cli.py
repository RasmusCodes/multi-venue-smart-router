"""Command line: ``python -m smartrouter <command>``.

    run         one parent order, metrics as JSON
    calibrate   fit venue statistics on training seeds
    experiment  run E1..E6 (or all) on evaluation seeds
    validate    stylised facts of the background market -> docs/VALIDATION.md
    report      tables and figures from results/ -> reports/
    bundle      package the engine for the browser demo (Pyodide)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def cmd_run(a):
    from .calibrate import load
    from .config import ParentConfig, preset
    from .episode import run_episode

    sc = preset(a.scenario)
    parent = ParentConfig(**{**sc.parent.to_dict(), **({"qty": a.qty} if a.qty else {}),
                             **({"slices": a.slices} if a.slices else {})})
    stats = load(a.stats) if a.stats else None
    if stats is None and a.strategy in ("model", "post_queue", "ck", "smart"):
        path = RESULTS / "stats" / f"{a.scenario}.json"
        if path.exists():
            stats = load(path)
    res = run_episode(sc, a.strategy, a.seed, stats=stats, parent=parent)
    print(json.dumps(res, indent=1))


def cmd_calibrate(a):
    from .calibrate import calibrate, save, summary
    from .config import preset

    sc = preset(a.scenario)
    st = calibrate(sc, n_single=a.single, n_joint=a.joint, n_take=a.take, workers=a.workers, log=_log)
    out = Path(a.out) if a.out else RESULTS / "stats" / f"{a.scenario}.json"
    save(st, out)
    print(json.dumps(summary(st, sc), indent=1))


def cmd_experiment(a):
    from .experiments import EXPERIMENTS, run_experiment

    keys = list(EXPERIMENTS) if a.key == "all" else a.key.split(",")
    for k in keys:
        _log(f"experiment {k}: {EXPERIMENTS[k]['title']}")
        run_experiment(k, RESULTS, RESULTS / "stats", seeds=a.seeds, seed0=a.seed0, workers=a.workers, log=_log,
                       calib_sizes=(a.single, a.joint, a.take))


def cmd_validate(a):
    from .validate import measure, to_markdown

    v = measure(a.scenario, seeds=a.seeds)
    (RESULTS).mkdir(exist_ok=True)
    with open(RESULTS / f"validation_{a.scenario}.json", "w", encoding="utf-8") as f:
        json.dump(v, f, indent=1)
    md = to_markdown(v)
    print(md)


def cmd_report(a):
    from .report import build_report

    build_report(RESULTS, ROOT / "reports", log=_log)


def cmd_bundle(a):
    from .bundle import build_bundle

    build_bundle(Path(a.out), RESULTS, log=_log)


def main(argv=None):
    p = argparse.ArgumentParser(prog="smartrouter", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scenario", default="baseline")
    r.add_argument("--strategy", default="smart")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--qty", type=int)
    r.add_argument("--slices", type=int)
    r.add_argument("--stats")
    r.set_defaults(fn=cmd_run)
    for name, fn in (("calibrate", cmd_calibrate), ("experiment", cmd_experiment)):
        c = sub.add_parser(name)
        if name == "calibrate":
            c.add_argument("--scenario", default="baseline")
            c.add_argument("--out")
        else:
            c.add_argument("key", help="E1..E6, comma-separated, or all")
            c.add_argument("--seeds", type=int)
            c.add_argument("--seed0", type=int, default=1)
        c.add_argument("--single", type=int, default=200)
        c.add_argument("--joint", type=int, default=200)
        c.add_argument("--take", type=int, default=100)
        c.add_argument("--workers", type=int)
        c.set_defaults(fn=fn)
    v = sub.add_parser("validate")
    v.add_argument("--scenario", default="baseline")
    v.add_argument("--seeds", type=int, default=20)
    v.set_defaults(fn=cmd_validate)
    rp = sub.add_parser("report")
    rp.set_defaults(fn=cmd_report)
    b = sub.add_parser("bundle")
    b.add_argument("--out", default=str(ROOT / "web" / "dist"))
    b.set_defaults(fn=cmd_bundle)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main(sys.argv[1:])
