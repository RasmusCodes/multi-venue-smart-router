"""Stylised facts of the simulated background market (no router).

Runs seeds of the background alone and measures what a market microstructure
reader would check first: spread, how often each venue sits at the NBBO and
with how much depth, volume shares, the share of volume that comes from
latency arbitrage, quote fades, and volatility.
"""

from __future__ import annotations

import math

from .book import BUY, DARKBG, LP, NOISE, SELL, SNIPER
from .config import NS_PER_S, preset
from .market import Market

VALIDATION_SEED0 = 700_000


def measure(scenario_name="baseline", seeds=20, seconds=360.0, sample_ms=100):
    sc = preset(scenario_name)
    names = [v.name for v in sc.venues]
    lit = [i for i, v in enumerate(sc.venues) if v.kind == "lit"]
    acc = {
        "samples": 0, "one_tick": 0, "spread_sum": 0.0,
        "at_bid": {i: 0 for i in lit}, "at_ask": {i: 0 for i in lit},
        "depth": {i: [0, 0] for i in lit}, "volume": {i: 0 for i in range(len(names))},
        "by_taker": {}, "fades": 0, "faded_qty": 0, "jumps": 0, "seconds": 0.0, "ret_1m": [],
    }
    step = sample_ms * 1_000_000
    warm = int(sc.market.warmup_s * NS_PER_S)
    end = int(seconds * NS_PER_S)
    for k in range(seeds):
        m = Market(sc, VALIDATION_SEED0 + k)
        t = 0
        last_min, last_mid = None, None
        while t < end:
            t += step
            m.run(t)
            if t < warm:
                continue
            bb, ba = m.nbbo()
            if bb is None or ba is None or ba <= bb:
                continue
            acc["samples"] += 1
            acc["one_tick"] += (ba - bb) == 100
            acc["spread_sum"] += (ba - bb) / ((ba + bb) / 2) * 1e4
            for i in lit:
                bk = m.venues[i].book
                if bk.best_bid() == bb:
                    acc["at_bid"][i] += 1
                    acc["depth"][i][0] += bk.qty_at(BUY, bb)
                    acc["depth"][i][1] += 1
                if bk.best_ask() == ba:
                    acc["at_ask"][i] += 1
                    acc["depth"][i][0] += bk.qty_at(SELL, ba)
                    acc["depth"][i][1] += 1
            minute = (t - warm) // (60 * NS_PER_S)
            if minute != last_min:
                mid = (bb + ba) / 2
                if last_mid is not None:
                    acc["ret_1m"].append(math.log(mid / last_mid))
                last_min, last_mid = minute, mid
        for v in m.venues:
            acc["volume"][v.idx] += v.volume
        for (tk, _mk), q in m.volume_by_owner.items():
            acc["by_taker"][tk] = acc["by_taker"].get(tk, 0) + q
        acc["fades"] += m.fade_index
        acc["faded_qty"] += m.faded_qty
        acc["jumps"] += m.jump_index
        acc["seconds"] += seconds
    n = acc["samples"]
    vol_total = sum(acc["volume"].values())
    taker_total = sum(acc["by_taker"].values())
    r = acc["ret_1m"]
    sd_1m = math.sqrt(sum(x * x for x in r) / len(r)) if r else 0.0
    out = {
        "scenario": scenario_name, "seeds": seeds, "seconds_per_seed": seconds,
        "one_tick_share": acc["one_tick"] / n, "mean_spread_bps": acc["spread_sum"] / n,
        "venues": {}, "taker_share": {}, "dark_volume_share": 0.0,
        "fades_per_min": acc["fades"] / (acc["seconds"] / 60), "jumps_per_min": acc["jumps"] / (acc["seconds"] / 60),
        "daily_vol_pct": sd_1m * math.sqrt(390) * 100,
    }
    for i in lit:
        d, c = acc["depth"][i]
        out["venues"][names[i]] = {
            "at_nbbo_bid": acc["at_bid"][i] / n, "at_nbbo_ask": acc["at_ask"][i] / n,
            "depth_at_nbbo": d / c if c else 0.0, "volume_share": acc["volume"][i] / vol_total,
            "make_fee": sc.venues[i].make_fee, "take_fee": sc.venues[i].take_fee,
        }
    for i, v in enumerate(sc.venues):
        if v.kind == "dark":
            out["dark_volume_share"] += acc["volume"][i] / vol_total
    labels = {NOISE: "uninformed takers", SNIPER: "latency arbitrage", DARKBG: "dark-pool arrivals",
              LP: "liquidity providers"}
    for tk, q in sorted(acc["by_taker"].items()):
        out["taker_share"][labels.get(tk, str(tk))] = q / taker_total
    return out


def to_markdown(v: dict) -> str:
    lines = [
        f"Background market only (no router), {v['seeds']} seeds x {v['seconds_per_seed']:.0f} s, "
        f"sampled every 100 ms after the warm-up.",
        "",
        f"- One-tick spread {v['one_tick_share']:.1%} of the time; mean quoted spread {v['mean_spread_bps']:.2f} bps.",
        f"- Volatility about {v['daily_vol_pct']:.1f}% a day (from one-minute mid returns, 390-minute day).",
        f"- {v['jumps_per_min']:.0f} news jumps and {v['fades_per_min']:.0f} cross-venue quote fades per minute.",
        f"- Dark pool share of volume {v['dark_volume_share']:.1%}.",
        "",
        "| Venue | Maker fee | Taker fee | At NBBO bid | At NBBO ask | Depth when at NBBO | Volume share |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, d in v["venues"].items():
        lines.append(f"| {name} | {d['make_fee'] / 1e4:+.4f} | {d['take_fee'] / 1e4:+.4f} | {d['at_nbbo_bid']:.0%} | "
                     f"{d['at_nbbo_ask']:.0%} | {d['depth_at_nbbo']:,.0f} | {d['volume_share']:.1%} |")
    lines += ["", "| Aggressor | Share of volume |", "|---|---:|"]
    for k, s in v["taker_share"].items():
        lines.append(f"| {k} | {s:.1%} |")
    return "\n".join(lines)
