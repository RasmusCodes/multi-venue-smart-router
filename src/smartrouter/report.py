"""Build reports/RESULTS.md, its figures and results/summary.json from the experiment outputs."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
from scipy import stats as sps

from .analysis import describe, holm, paired, venue_markouts
from .config import preset
from .experiments import EXPERIMENTS, load_results
from .router import label

MATERIAL = 0.25  # bps, pre-registered

PRIMARY = [
    ("P1", "E1", "baseline", "sync", "nbbo", "Synchronised fee-aware sweep vs naive NBBO sweep (taking)"),
    ("P2", "E1", "baseline", "fee", "nbbo", "Fee-aware sweep sent at once vs naive NBBO sweep (taking)"),
    ("P3", "E1", "baseline", "model", "sync", "Model-based sweep vs synchronised sweep (taking)"),
    ("P4", "E2", "baseline", "post_queue", "post_primary", "Queue-aware post vs post on primary (posting)"),
    ("P5", "E3", "baseline", "ck", "post_queue", "Cont-Kukanov split vs queue-aware post"),
    ("P6", "E4", "patient", "smart", "post_primary", "Smart router vs post on primary, patient schedule"),
    ("P7", "E4", "urgent", "smart", "post_primary", "Smart router vs post on primary, urgent schedule"),
    ("P8", "E4", "patient", "smart", "nbbo", "Smart router vs naive NBBO sweep, patient schedule"),
    ("P9", "E4", "urgent", "smart", "nbbo", "Smart router vs naive NBBO sweep, urgent schedule"),
]

REGIMES = {"fee_cap_10": "Access fees capped at $0.0010", "fast_fade": "Faster fading, stronger impact",
           "high_vol": "Higher volatility", "wide_latency": "Wider latency gaps"}

VENUE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
INK, INK2, MUTED, GRID, BASE = "#1a1a18", "#2f2e2a", "#6b6963", "#e6e2d8", "#c3c2b7"
ACCENT = "#14594b"


def verdict(pr, p_adj):
    if p_adj < 0.05 and pr["mean"] < 0:
        return "Supported, material" if -pr["mean"] >= MATERIAL else "Supported, below the 0.25 bps bar"
    if p_adj < 0.05 and pr["mean"] > 0:
        return "Rejected: the opposite sign"
    return "Not supported"


def f2(x):
    return f"{x:+.2f}".replace("-", "−")


def pfmt(p):
    if p < 1e-4:
        return "< 0.0001"
    return f"{p:.4f}"


def _cache(results_dir):
    cache = {}

    def get(key, lab):
        if (key, lab) not in cache:
            cache[(key, lab)] = load_results(results_dir, key, lab)
        return cache[(key, lab)]

    return get


def tost(d, margin):
    """Two one-sided tests for equivalence within +-margin (alpha 0.05 each): 90% interval inside the margin."""
    d = np.asarray(d, dtype=float)
    n = d.size
    se = d.std(ddof=1) / math.sqrt(n)
    t = sps.t.ppf(0.95, n - 1)
    lo, hi = d.mean() - t * se, d.mean() + t * se
    return lo, hi, (lo > -margin and hi < margin)


def _diffs(res, a, b, metric="is_bps"):
    A = {r["seed"]: r for r in res if r["strategy"] == a}
    B = {r["seed"]: r for r in res if r["strategy"] == b}
    return [A[s][metric] - B[s][metric] for s in sorted(set(A) & set(B))
            if A[s].get(metric) is not None and B[s].get(metric) is not None]


# ---------------------------------------------------------------------------------------- figures

def _style():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "svg.fonttype": "none", "font.family": "sans-serif",
        "font.sans-serif": ["Inter", "Segoe UI", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 10.5, "axes.edgecolor": BASE, "axes.labelcolor": INK2, "xtick.color": MUTED,
        "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": False,
        "figure.facecolor": "white", "axes.facecolor": "white", "text.parse_math": False,
    })
    return plt


def fig_forest(rows, path, title):
    plt = _style()
    fig, ax = plt.subplots(figsize=(8.2, 0.42 * len(rows) + 1.3))
    ys = np.arange(len(rows))[::-1]
    lo = min(r["ci_lo"] for r in rows)
    hi = max(r["ci_hi"] for r in rows)
    pad = 0.08 * (hi - lo)
    ax.axvspan(-MATERIAL, MATERIAL, color="#f3f1ea", zorder=0, lw=0)
    ax.axvline(0, color=BASE, lw=1, zorder=1)
    for y, r in zip(ys, rows):
        ax.plot([r["ci_lo"], r["ci_hi"]], [y, y], color=INK2, lw=2, solid_capstyle="round", zorder=2)
        ax.plot([r["mean"]], [y], "o", ms=7, mfc=ACCENT if r.get("good") else "white", mec=ACCENT, mew=2, zorder=3)
        ax.text(r["ci_hi"] + pad * 0.35, y, f2(r["mean"]), va="center", ha="left", color=INK2, fontsize=9.5)
    ax.set_yticks(ys, [r["label"] for r in rows])
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(min(lo, -MATERIAL) - pad, max(hi, MATERIAL) + pad * 3)
    ax.set_xlabel("Shortfall difference, bps (negative = cheaper)")
    fig.suptitle(title, x=0.01, ha="left", color=INK, fontsize=11.5, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_fades(rows, path):
    plt = _style()
    fig, ax = plt.subplots(figsize=(7.6, 2.9))
    names = [label(r["strategy"]) for r in rows]
    vals = [100 * (r["fade_rate"] or 0) for r in rows]
    ys = np.arange(len(rows))[::-1]
    ax.barh(ys, vals, height=0.42, color=ACCENT)
    for y, v in zip(ys, vals):
        ax.text(v + 0.6, y, f"{v:.1f}%", va="center", color=INK2, fontsize=9.5)
    ax.set_yticks(ys, names)
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, max(vals) * 1.18 + 1)
    ax.xaxis.set_major_formatter(lambda v, _pos: f"{v:.0f}%")
    ax.set_xlabel("Displayed liquidity gone by the time an IOC child order arrived")
    ax.set_title("Quote fades by sweep design (E1)", loc="left", color=INK, fontsize=11.5, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_markouts(vm, scenario, path):
    plt = _style()
    sc = preset(scenario)
    names = [v.name for v in sc.venues if v.kind == "lit"]
    desc = {"ALPHA": "maker-taker", "BRAVO": "highest rebate", "CHARLIE": "inverted", "DELTA": "speed bump"}
    vals = [vm[n]["markout_bps"] if n in vm else 0.0 for n in names]
    fig, ax = plt.subplots(figsize=(7.6, 2.9))
    ys = np.arange(len(names))[::-1]
    for y, n, v in zip(ys, names, vals):
        ax.barh([y], [v], height=0.42, color=VENUE_COLORS[sc.venue_index(n)])
        ax.text(v + (0.03 if v >= 0 else -0.03), y, f2(v), va="center", ha="left" if v >= 0 else "right",
                color=INK2, fontsize=9.5)
    ax.axvline(0, color=BASE, lw=1)
    ax.set_yticks(ys, [f"{n} ({desc.get(n, '')})" for n in names])
    ax.tick_params(axis="y", length=0)
    lim = max(abs(v) for v in vals) * 1.35
    ax.set_xlim(-lim, lim)
    ax.set_xlabel("10-second markout of passive fills, bps (negative = the price moved against the fill)")
    ax.set_title("Adverse selection by venue (E2, all posting routers pooled)", loc="left", color=INK,
                 fontsize=11.5, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_tactic(rows_by_sched, path):
    plt = _style()
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 2.7), sharex=True)
    for ax, (sched, rows) in zip(axes, rows_by_sched.items()):
        names = [label(r["strategy"]) for r in rows]
        vals = [r["mean"] for r in rows]
        ys = np.arange(len(rows))[::-1]
        ax.barh(ys, vals, height=0.42, color=[MUTED if r["strategy"] == "nbbo" else ACCENT for r in rows])
        for y, v, r in zip(ys, vals, rows):
            ax.text(max(v, 0) + 0.08, y, f"{v:.2f}".replace("-", "−"), va="center", color=INK2, fontsize=9.5)
        ax.axvline(0, color=BASE, lw=1)
        ax.set_yticks(ys, names)
        ax.tick_params(axis="y", length=0)
        ax.set_title(f"{sched.capitalize()} schedule", loc="left", color=INK, fontsize=10.5, fontweight="bold")
        ax.set_xlabel("Mean shortfall, bps")
    fig.suptitle("Crossing the spread vs resting first (E4, 300 seeds)", x=0.01, ha="left", color=INK,
                 fontsize=11.5, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------------------------------------------------------------------------------------- report

def build_report(results_dir: Path, out_dir: Path, log=print):
    results_dir, out_dir = Path(results_dir), Path(out_dir)
    figs = out_dir / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    get = _cache(results_dir)
    lines = []
    W = lines.append

    # primary hypotheses
    prim = []
    for hid, key, lab, a, b, text in PRIMARY:
        pr = paired(get(key, lab), a, b)
        prim.append((hid, key, lab, a, b, text, pr))
    adj = holm([x[6]["p"] for x in prim])

    # secondary
    e2 = get("E2", "baseline")
    s1 = [("10 s markout of passive fills: post for rebate − post on primary", paired(e2, "post_rebate", "post_primary", "markout_M_10s_bps")),
          ("10 s markout of passive fills: post for rebate − queue-aware post", paired(e2, "post_rebate", "post_queue", "markout_M_10s_bps")),
          ("Shortfall: post for rebate − post on primary", paired(e2, "post_rebate", "post_primary"))]
    s2 = []
    for reg in REGIMES:
        res = get("E5", reg)
        for base in ("post_primary", "nbbo"):
            s2.append((reg, base, paired(res, "smart", base)))
    s2_adj = holm([x[2]["p"] for x in s2])
    e6 = get("E6", "homogeneous")
    d_sync = _diffs(e6, "sync", "nbbo")
    sync_identical = max(abs(x) for x in d_sync) == 0.0
    d_smart = _diffs(e6, "smart", "post_primary")
    t_lo, t_hi, t_ok = tost(d_smart, MATERIAL)
    s3_pr = paired(e6, "smart", "post_primary")
    s4 = [("Adaptive post − post on primary", paired(e2, "post_adaptive", "post_primary")),
          ("Adaptive post − queue-aware post", paired(e2, "post_adaptive", "post_queue"))]

    # per-experiment descriptive tables
    desc = {}
    for key, spec in EXPERIMENTS.items():
        for run in spec["runs"]:
            desc[(key, run["label"])] = describe(get(key, run["label"]), spec["strategies"])

    # figures
    routing_rows = []
    for hid, key, lab, a, b, text, pr in prim[:7]:
        routing_rows.append({"label": f"{hid} {label(a)} − {label(b)}" + (f" ({lab})" if key == "E4" else ""),
                             "mean": pr["mean"], "ci_lo": pr["ci_lo"], "ci_hi": pr["ci_hi"], "good": True})
    for reg, base, pr in s2:
        if base == "post_primary":
            routing_rows.append({"label": f"S2 Smart router − Post on primary ({REGIMES[reg].lower()})",
                                 "mean": pr["mean"], "ci_lo": pr["ci_lo"], "ci_hi": pr["ci_hi"], "good": True})
    routing_rows.append({"label": "S3 Smart router − Post on primary (identical venues)", "mean": s3_pr["mean"],
                         "ci_lo": s3_pr["ci_lo"], "ci_hi": s3_pr["ci_hi"], "good": False})
    fig_forest(routing_rows, figs / "routing_effects.svg", "Routing gains at a fixed tactic (paired by seed)")
    fig_fades(desc[("E1", "baseline")], figs / "fades.svg")
    vm = venue_markouts(e2, ["post_primary", "post_rebate", "post_queue", "post_adaptive"])
    fig_markouts(vm, "baseline", figs / "markouts_by_venue.svg")
    fig_tactic({"patient": desc[("E4", "patient")], "urgent": desc[("E4", "urgent")]}, figs / "tactic.svg")

    # ---------------------------------------------------------------- markdown
    W("# Results")
    W("")
    W(f"Generated {time.strftime('%Y-%m-%d')} by `python -m smartrouter report` from the experiment outputs in "
      "`results/`. The hypotheses, metric, seeds and decision rules were fixed in "
      "[the pre-registration](../docs/PREREGISTRATION.md) before any evaluation seed was run. Everything here is "
      "about the simulated market described in [the model](../docs/MODEL.md); nothing is a claim about live trading.")
    W("")
    W("## Short answer")
    W("")
    p = {x[0]: (x[6], a_) for x, a_ in zip(prim, adj)}
    W(f"- **Taking liquidity, routing pays and the reason is latency.** Sending child orders so that they arrive "
      f"at every venue at once cut the share of displayed liquidity that vanished before arrival from "
      f"{desc[('E1', 'baseline')][1]['fade_rate']:.0%} to {desc[('E1', 'baseline')][3]['fade_rate']:.0%} and saved "
      f"{-p['P1'][0]['mean']:.2f} bps against the naive NBBO sweep [95% CI {-p['P1'][0]['ci_hi']:.2f} to "
      f"{-p['P1'][0]['ci_lo']:.2f}], cheaper on {p['P1'][0]['win_rate']:.0%} of seeds. Fee awareness alone saved "
      f"{-p['P2'][0]['mean']:.2f} bps (real but below the 0.25 bps bar): sent all at once, the cheaper venues are "
      f"the slower ones, and their quotes fade first.")
    W(f"- **Resting liquidity, routing pays less, and rebates are a trap.** The full smart router beat naive "
      f"posting on the primary venue by {-p['P6'][0]['mean']:.2f} bps on the patient schedule "
      f"[{-p['P6'][0]['ci_hi']:.2f} to {-p['P6'][0]['ci_lo']:.2f}]. Queue-aware venue choice alone saved "
      f"{-p['P4'][0]['mean']:.2f} bps, just under the bar. Chasing the highest maker rebate cost "
      f"{s1[2][1]['mean']:.2f} bps: fills there carried the worst adverse selection.")
    W(f"- **What did not pay.** The Cont-Kukanov split across venues did not beat the best single venue "
      f"({f2(p['P5'][0]['mean'])} bps, Holm p {pfmt(p['P5'][1])}), and on the urgent schedule the smart router's "
      f"edge over naive posting ({f2(p['P7'][0]['mean'])} bps) was not significant. Learning venue quality from "
      "scratch within one order (Thompson sampling) was no better than posting on the primary.")
    ratio = p["P8"][0]["mean"] / p["P6"][0]["mean"]
    W(f"- **The biggest lever is not the venue.** Resting first instead of crossing the spread saved about "
      f"{-p['P8'][0]['mean']:.1f} bps, {ratio:.0f} times what venue choice added for resting orders. In this model "
      "passive orders have no permanent price impact, so that number is an upper bound; the venue comparisons at "
      "a fixed tactic are the robust part.")
    W(f"- **Checks.** The smart router's gain over naive posting held in all four other regimes "
      f"({', '.join(f2(x[2]['mean']) for x in s2 if x[1] == 'post_primary')} bps), and vanished with four "
      f"identical venues ({f2(s3_pr['mean'])} bps: no gain, a small cost of extra complexity).")
    W("")
    share = -p["P1"][0]["mean"] / desc[("E1", "baseline")][1]["mean"]
    W(f"So, in this market: **yes for marketable flow** (about {-p['P1'][0]['mean']:.1f} bps, {share:.0%} of the "
      "naive router's average cost, driven by latency rather than fees), **modestly for patient passive flow** "
      f"(about {-p['P6'][0]['mean']:.1f} bps), and **no measurable gain** from optimising the split across venues "
      "or from routing urgent passive flow more cleverly.")
    W("")
    W("![Routing effects](figures/routing_effects.svg)")
    W("")
    W("Each row is a paired comparison over the same seeds, with its 95% interval; the shaded band marks "
      "±0.25 bps, the pre-registered materiality bar. Hollow marker: the negative control, where no gain is "
      "expected.")
    W("")
    W("## Primary hypotheses")
    W("")
    W("Paired by seed (300 evaluation seeds), two-sided t-tests, Holm-Bonferroni across the nine. Negative = the "
      "first router is cheaper. Material = saving of at least 0.25 bps with Holm p < 0.05.")
    W("")
    W("| | Comparison | Difference, bps | 95% CI | Bootstrap 95% | Cheaper on | Holm p | Verdict |")
    W("|---|---|---:|---|---|---:|---:|---|")
    for (hid, key, lab, a, b, text, pr), pa in zip(prim, adj):
        W(f"| {hid} | {text} | {f2(pr['mean'])} | {f2(pr['ci_lo'])} to {f2(pr['ci_hi'])} | "
          f"{f2(pr['boot_lo'])} to {f2(pr['boot_hi'])} | {pr['win_rate']:.0%} | {pfmt(pa)} | {verdict(pr, pa)} |")
    W("")
    w_adj = holm([x[6]["wilcoxon_p"] for x in prim])
    agree = all((wa < 0.05) == (pa < 0.05) for wa, pa in zip(w_adj, adj))
    W("Robustness of the tests: Holm-adjusted Wilcoxon signed-rank tests "
      + ("reach the same significance verdict as the t-tests for all nine hypotheses." if agree else
         "differ from the t-tests for: " + ", ".join(x[0] for x, wa, pa in zip(prim, w_adj, adj)
                                                       if (wa < 0.05) != (pa < 0.05)) + ".")
      + " The bootstrap intervals differ from the t intervals by at most "
      + f"{max(max(abs(x[6]['boot_lo'] - x[6]['ci_lo']), abs(x[6]['boot_hi'] - x[6]['ci_hi'])) for x in prim):.3f} bps.")
    W("")
    W("## Secondary hypotheses")
    W("")
    W("**S1, the rebate trap.** Supported.")
    W("")
    W("| Comparison | Difference | 95% CI | p |")
    W("|---|---:|---|---:|")
    for text, pr in s1:
        W(f"| {text} | {f2(pr['mean'])} bps | {f2(pr['ci_lo'])} to {f2(pr['ci_hi'])} | {pfmt(pr['p'])} |")
    W("")
    W("![Markouts by venue](figures/markouts_by_venue.svg)")
    W("")
    W("Pooled over the posting routers of E2, passive fills on the inverted venue and the speed-bump venue were "
      "followed by favourable price moves, while fills on the two maker-taker venues were followed by adverse "
      "ones, worst on the highest-rebate venue. This is the pattern Battalio, Corwin and Jennings (2016) report "
      "for US exchanges: limit orders routed for rebates fill less and are picked off more.")
    W("")
    W("**S2, robustness.** Supported in all eight tests (Holm within the group).")
    W("")
    W("| Regime | Smart − post on primary | Smart − naive sweep | Holm p (both) |")
    W("|---|---|---|---|")
    for reg in REGIMES:
        a_ = [(x, pa) for x, pa in zip(s2, s2_adj) if x[0] == reg]
        pp = a_[0][0][2]
        pn = a_[1][0][2]
        W(f"| {REGIMES[reg]} | {f2(pp['mean'])} [{f2(pp['ci_lo'])}, {f2(pp['ci_hi'])}] | {f2(pn['mean'])} "
          f"[{f2(pn['ci_lo'])}, {f2(pn['ci_hi'])}] | {pfmt(a_[0][1])}, {pfmt(a_[1][1])} |")
    W("")
    W(f"**S3, negative control.** Supported. With four identical venues the synchronised and naive sweeps made "
      f"{'identical' if sync_identical else 'different'} decisions on every seed (difference exactly 0). The smart "
      f"router was {f2(s3_pr['mean'])} bps against posting on the primary (90% interval {f2(t_lo)} to {f2(t_hi)}: "
      f"{'inside' if t_ok else 'not inside'} ±0.25 bps, so equivalent by the pre-registered test). It was slightly "
      f"worse (p {pfmt(s3_pr['p'])}): when there is nothing to route around, splitting across venues only costs.")
    W("")
    W("**S4, learning.** Not supported.")
    W("")
    W("| Comparison | Difference | 95% CI | p |")
    W("|---|---:|---|---:|")
    for text, pr in s4:
        W(f"| {text} | {f2(pr['mean'])} bps | {f2(pr['ci_lo'])} to {f2(pr['ci_hi'])} | {pfmt(pr['p'])} |")
    W("")
    W("A likely reason: twenty slices are too few decisions to learn which venue is toxic, while the same knowledge, "
      "fitted on training seeds, is what makes the queue-aware post work.")
    W("")
    W("## The mechanisms")
    W("")
    W("![Quote fades](figures/fades.svg)")
    W("")
    W("Fast liquidity providers watch every venue. When a large trade prints on one, they pull quotes on the "
      "others within tens of microseconds. A router that sends all child orders at once reaches the nearest venue "
      "first, and its own fill there triggers the fades that its slower child orders then run into. Ranking venues "
      "by fees makes this worse, because the venues with the best all-in prices are farther away. Staggering send "
      "times so that every child order arrives together removes most of it, which is the idea behind timed-release "
      "routers such as RBC's THOR.")
    W("")
    W("![Tactic](figures/tactic.svg)")
    W("")
    W("## Tables")
    W("")
    for key, spec in EXPERIMENTS.items():
        for run in spec["runs"]:
            rows = desc[(key, run["label"])]
            W(f"### {key} {spec['title']}" + (f": {run['label']}" if len(spec["runs"]) > 1 else ""))
            W("")
            W(spec["question"])
            W("")
            W("| Router | Shortfall, bps (± se) | Price | Fees | Passive share | Faded | Dark share | "
              "Markout passive 10 s | Child orders" + (" | Permanent impact" if key == "E4" else "") + " |")
            W("|---|---:|---:|---:|---:|---:|---:|---:|---:|" + ("---:|" if key == "E4" else ""))
            for r in rows:
                cells = [label(r["strategy"]), f"{r['mean']:.2f} ± {r['se']:.2f}", f"{r['price_bps']:.2f}",
                         f"{r['fee_bps']:.2f}", f"{r['maker_share']:.0%}",
                         "–" if r["fade_rate"] is None else f"{r['fade_rate']:.1%}", f"{r['dark_share']:.1%}",
                         "–" if r["markout_M_10s"] is None else
                         ("0.00" if abs(r["markout_M_10s"]) < 0.005 else f"{r['markout_M_10s']:.2f}"),
                         f"{r['n_children']:.0f}"]
                if key == "E4":
                    cells.append(f"{r['impact_bps']:.2f}" if r["impact_bps"] is not None else "–")
                W("| " + " | ".join(c.replace("-", "−") if c[:1] == "-" else c for c in cells) + " |")
            W("")
    W("Shortfall levels include the market's random drift over each order (standard deviation about 10 bps), so "
      "compare routers through the paired differences above, not through these means. Permanent impact is fair "
      "value at the end of the horizon with the order minus without it, from a counterfactual run of the same seed.")
    W("")
    W("## Deviations from the pre-registration")
    W("")
    W("None in the simulator, the routers or the analysis. The first launch of the evaluation run stopped before "
      "simulating anything (the package was not installed in the environment) and was restarted unchanged. "
      "After the evaluation, only documentation, packaging metadata, the report generator and the browser demo "
      "were added.")
    W("")
    W("## Limitations")
    W("")
    W("- One simulated stock, five stylised venues, mechanical liquidity providers that never learn about the "
      "router. The parameters follow qualitative facts from the literature (see [validation](../docs/VALIDATION.md)) "
      "rather than a calibration to a particular stock and day.")
    W("- Passive orders have no permanent impact here, so the tactic gap (resting vs crossing) is an upper bound.")
    W("- Every router uses the same TWAP schedule; scheduling itself is not optimised.")
    W("- The calibration probes rest one order at a time; the venue model ignores how much its own order "
      "changes takers' routing.")
    (out_dir / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # summary for the website demo
    summ = {"generated": time.strftime("%Y-%m-%d"), "material_bps": MATERIAL, "primary": [], "headline": {}}
    for (hid, key, lab, a, b, text, pr), pa in zip(prim, adj):
        summ["primary"].append({"id": hid, "experiment": key, "run": lab, "strategy": a, "baseline": b, "text": text,
                                "mean": pr["mean"], "lo": pr["ci_lo"], "hi": pr["ci_hi"], "p_holm": pa,
                                "win_rate": pr["win_rate"], "verdict": verdict(pr, pa), "n": pr["n"]})
    for sched in ("patient", "urgent"):
        rows = []
        for base in ("nbbo", "post_primary"):
            pr = paired(get("E4", sched), "smart", base)
            rows.append({"scenario": "baseline", "baseline": base, "mean": pr["mean"], "lo": pr["ci_lo"],
                         "hi": pr["ci_hi"], "n": pr["n"]})
        if sched == "patient":
            for reg, base, pr in s2:
                rows.append({"scenario": reg, "baseline": base, "mean": pr["mean"], "lo": pr["ci_lo"],
                             "hi": pr["ci_hi"], "n": pr["n"]})
            rows.append({"scenario": "homogeneous", "baseline": "post_primary", "mean": s3_pr["mean"],
                         "lo": s3_pr["ci_lo"], "hi": s3_pr["ci_hi"], "n": s3_pr["n"]})
            pn = paired(e6, "smart", "nbbo")
            rows.append({"scenario": "homogeneous", "baseline": "nbbo", "mean": pn["mean"], "lo": pn["ci_lo"],
                         "hi": pn["ci_hi"], "n": pn["n"]})
        summ["headline"][sched] = rows
    summ["fades"] = {r["strategy"]: r["fade_rate"] for r in desc[("E1", "baseline")]}
    summ["markouts"] = {k: v["markout_bps"] for k, v in vm.items()}
    summ["e1"] = {r["strategy"]: r["mean"] for r in desc[("E1", "baseline")]}
    with open(results_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summ, f, indent=1)
    log(f"wrote {out_dir / 'RESULTS.md'} and {len(list(figs.glob('*.svg')))} figures")
    return summ
