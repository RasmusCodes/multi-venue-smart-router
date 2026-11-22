"""Package the engine and its fitted statistics for the browser demo.

Writes into ``out``:

* ``engine.zip``   - the simulator and routers (pure Python) plus the venue
                     statistics each scenario's model-based routers use; the
                     page's Web Worker unpacks it into Pyodide's file system;
* ``summary.json`` - headline results of the pre-registered experiments, drawn
                     by the page before Pyodide has loaded;
* the demo's own files from ``web/`` (script, worker, stylesheets) and a
  stand-alone page, ``index.html``, that shows the demo on its own.

Joint scenarios are cut to the 150 the Cont-Kukanov router reads, which keeps
the demo's decisions identical to the experiments' for the same seed.
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parents[1]
MODULES = ["__init__.py", "detmath.py", "config.py", "book.py", "venue.py", "market.py", "metrics.py",
           "episode.py", "demo.py", "router/__init__.py", "router/context.py", "router/executor.py",
           "router/explore.py", "router/policies.py", "router/ck.py", "router/registry.py"]
WEB_FILES = ["sor-demo.js", "sor-worker.js", "sor-demo.css", "index.html", "standalone.css"]


def _trim(stats, n_joint=150):
    return {
        "scenario": stats["scenario"], "wait_s": stats["wait_s"], "survival": stats["survival"],
        "cleanup_fee": stats["cleanup_fee"], "post": stats["post"], "joint": stats["joint"][:n_joint],
    }


def build_bundle(out: Path, results: Path, log=print):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in MODULES:
            data = (PKG / rel).read_bytes()
            info = zipfile.ZipInfo(f"smartrouter/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
        stats_dir = results / "stats"
        for path in sorted(stats_dir.glob("*.json")):
            st = json.loads(path.read_text(encoding="utf-8"))
            data = json.dumps(_trim(st), separators=(",", ":")).encode()
            info = zipfile.ZipInfo(f"smartrouter_data/stats/{path.name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    (out / "engine.zip").write_bytes(buf.getvalue())
    log(f"engine.zip: {len(buf.getvalue()) / 1024:.0f} KB")
    summary = results / "summary.json"
    if summary.exists():
        shutil.copyfile(summary, out / "summary.json")
    for name in WEB_FILES:
        src = ROOT / "web" / name
        if src.exists():
            shutil.copyfile(src, out / name)
    log(f"bundle written to {out}")
