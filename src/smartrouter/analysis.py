"""Paired statistics over seeds (needs numpy and scipy, the ``research`` extra)."""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps

BOOT = 10_000


def by_strategy(results):
    out = {}
    for r in results:
        out.setdefault(r["strategy"], {})[r["seed"]] = r
    return out


def _val(r, metric):
    v = r.get(metric)
    return np.nan if v is None else float(v)


def describe(results, strategies, metric="is_bps"):
    """Per-strategy summary of a metric and the main diagnostics."""
    bs = by_strategy(results)
    rows = []
    for s in strategies:
        rs = list(bs.get(s, {}).values())
        if not rs:
            continue
        x = np.array([_val(r, metric) for r in rs])
        x = x[~np.isnan(x)]

        def mean(key, rs=rs):
            v = np.array([_val(r, key) for r in rs])
            v = v[~np.isnan(v)]
            return float(v.mean()) if v.size else None

        rows.append({
            "strategy": s, "n": int(x.size), "mean": float(x.mean()), "se": float(x.std(ddof=1) / math.sqrt(x.size)),
            "sd": float(x.std(ddof=1)), "median": float(np.median(x)),
            "price_bps": mean("price_bps"), "fee_bps": mean("fee_bps"), "opp_bps": mean("opp_bps"),
            "maker_share": mean("maker_share"), "fade_rate": mean("fade_rate"),
            "take_fill_ratio": mean("take_fill_ratio"),
            "dark_share": float(np.mean([r["dark_fill"] / r["qty"] for r in rs])),
            "markout_M_10s": mean("markout_M_10s_bps"), "markout_T_10s": mean("markout_T_10s_bps"),
            "markout_M_60s": mean("markout_M_60s_bps"),
            "complete_s": mean("complete_s"), "n_children": mean("n_children"),
            "impact_bps": mean("impact_bps"),
        })
    return rows


def paired(results, strategy, baseline, metric="is_bps", seed=12345):
    """Paired difference strategy - baseline over common seeds (negative = cheaper than baseline)."""
    bs = by_strategy(results)
    a, b = bs.get(strategy, {}), bs.get(baseline, {})
    seeds = sorted(set(a) & set(b))
    d = np.array([_val(a[s], metric) - _val(b[s], metric) for s in seeds])
    d = d[~np.isnan(d)]
    n = d.size
    if n < 3:
        return None
    mean = float(d.mean())
    sd = float(d.std(ddof=1))
    se = sd / math.sqrt(n)
    if se > 0:
        t = mean / se
        p = float(2 * sps.t.sf(abs(t), n - 1))
        half = float(sps.t.ppf(0.975, n - 1) * se)
    else:
        t, p, half = (math.inf if mean else 0.0), (0.0 if mean else 1.0), 0.0
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(BOOT, n))
    boot = d[idx].mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    w = sps.wilcoxon(d) if np.any(d != 0) else None
    return {
        "strategy": strategy, "baseline": baseline, "metric": metric, "n": int(n), "mean": mean, "sd": sd, "se": se,
        "ci_lo": mean - half, "ci_hi": mean + half, "boot_lo": float(lo), "boot_hi": float(hi), "t": float(t),
        "p": p, "wilcoxon_p": float(w.pvalue) if w is not None else 1.0,
        "win_rate": float(np.mean(d < 0)), "tie_rate": float(np.mean(d == 0)),
    }


def holm(pvals):
    """Holm-Bonferroni adjusted p-values (same order as the input)."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        val = min(1.0, (m - rank) * pvals[i])
        running = max(running, val)
        adj[i] = running
    return adj


def venue_markouts(results, strategies):
    """Quantity-weighted 10 s markout of passive fills per venue (bps of arrival mid), pooled over seeds."""
    out = {}
    for r in results:
        if r["strategy"] not in strategies:
            continue
        for v, (s, q) in r.get("venue_maker_mk10", {}).items():
            acc = out.setdefault(v, [0.0, 0, 0.0])
            acc[0] += s
            acc[1] += q
            acc[2] += q * r["arrival_mid"]
    # sum(q * markout_pu) / sum(q * mid) = quantity-weighted markout in units of the mid
    return {v: {"qty": q, "markout_bps": (s / w * 1e4) if w else None} for v, (s, q, w) in sorted(out.items())}
