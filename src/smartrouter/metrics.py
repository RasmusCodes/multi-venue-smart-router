"""Execution-quality metrics for one parent order.

Implementation shortfall (IS) is measured against the arrival mid, the NBBO
midpoint when the parent order arrived, in basis points of that mid, and
includes fees and rebates:

    cost per share = side * (fill price - arrival mid) + fee per share

(fee negative for a rebate). It splits into a price component and a fee
component. Markouts use the simulator's latent fair value F:
side * (F(t + h) - fill price), positive when the price moved the trade's
way afterwards; for passive fills a negative markout is adverse selection.
"""

from __future__ import annotations

from .book import BUY
from .config import NS_PER_S

MARKOUT_H = (1, 10, 60)


def _bps(x, ref):
    return x / ref * 1e4 if ref else 0.0


def summarize(market, strategy, wall_s=None) -> dict:
    p = market.parent
    ctx = market.ctx
    mid0 = float(p.arrival_mid)
    side = p.side
    fills = market.rfills
    filled = 0
    cost_px = 0.0
    cost_fee = 0.0
    maker = 0
    venue_qty = {}
    venue_maker = {}
    mk = {("M", h): [0.0, 0] for h in MARKOUT_H}
    mk.update({("T", h): [0.0, 0] for h in MARKOUT_H})
    venue_mk10 = {}  # maker fills: venue -> [sum of qty * markout (pu), qty]
    last_t = p.t0
    for (t, vi, s, px, q, liq, fee, _coid) in fills:
        filled += q
        cost_px += q * side * (px - mid0)
        cost_fee += q * fee
        if liq == "M":
            maker += q
            venue_maker[vi] = venue_maker.get(vi, 0) + q
        venue_qty[vi] = venue_qty.get(vi, 0) + q
        last_t = max(last_t, t)
        for h in MARKOUT_H:
            acc = mk[(liq, h)]
            m_h = q * side * (market.fair_at(t + h * NS_PER_S) - px)
            acc[0] += m_h
            acc[1] += q
            if liq == "M" and h == 10:
                vm = venue_mk10.setdefault(market.venues[vi].name, [0.0, 0])
                vm[0] += m_h
                vm[1] += q
    unfilled = p.qty - filled
    end_mid = market.mid() or mid0
    opp = 0.0
    if unfilled > 0:  # value what is left at the end mid plus half a tick and a typical fee
        opp = unfilled * (side * (end_mid - mid0) + 50 + 15)
    denom = p.qty * mid0
    out = {
        "strategy": strategy.name,
        "seed": market.seed,
        "side": side,
        "qty": p.qty,
        "filled": filled,
        "unfilled": unfilled,
        "arrival_mid": mid0,
        "arrival_fair": p.arrival_fair,
        "is_bps": (cost_px + cost_fee + opp) / denom * 1e4,
        "price_bps": cost_px / denom * 1e4,
        "fee_bps": cost_fee / denom * 1e4,
        "opp_bps": opp / denom * 1e4,
        "maker_share": maker / filled if filled else 0.0,
        "vwap": (sum(q * px for (_, _, _, px, q, _, _, _) in fills) / filled) if filled else None,
        "fee_ps": cost_fee / filled if filled else 0.0,
        "complete_s": ((strategy.done_t - p.t0) / NS_PER_S) if getattr(strategy, "done_t", None) else None,
        "last_fill_s": (last_t - p.t0) / NS_PER_S,
        "venue_qty": {market.venues[k].name: v for k, v in sorted(venue_qty.items())},
        "venue_maker": {market.venues[k].name: v for k, v in sorted(venue_maker.items())},
        "venue_maker_mk10": venue_mk10,
        "fair_end_move_bps": side * _bps(market.fair_at(p.end_ns) - p.arrival_fair, mid0),
        "fair_end": market.fair_at(p.end_ns),
        "end_ns": p.end_ns,
    }
    for (liq, h), (s_, q_) in mk.items():
        out[f"markout_{liq}_{h}s_bps"] = _bps(s_ / q_, mid0) if q_ else None
    # child-order statistics
    take_req = take_fill = take_seen = 0
    dark_req = dark_fill = 0
    posts = post_fill = 0
    n_take = 0
    fade_num = fade_den = 0
    for ch in ctx.children.values():
        if ch.kind == "take":
            n_take += 1
            take_req += ch.qty
            take_fill += ch.filled
            take_seen += ch.snap_qty
            expect = min(ch.qty, ch.snap_qty) if ch.snap_qty else ch.qty
            fade_den += expect
            fade_num += max(0, expect - ch.filled)
        elif ch.kind == "dark":
            dark_req += ch.qty
            dark_fill += ch.filled
        elif ch.kind == "post":
            posts += 1
            post_fill += ch.filled
    out.update({
        "n_children": len(ctx.children),
        "n_cancels": ctx.n_cancels,
        "n_take": n_take,
        "take_fill_ratio": take_fill / take_req if take_req else None,
        "fade_rate": fade_num / fade_den if fade_den else None,
        "dark_req": dark_req,
        "dark_fill": dark_fill,
        "n_posts": posts,
        "post_fill_qty": post_fill,
        "events": market.n_events,
    })
    if hasattr(strategy, "n_ck"):
        out["n_ck"] = strategy.n_ck
    if wall_s is not None:
        out["wall_s"] = wall_s
    return out


def side_name(side):
    return "buy" if side == BUY else "sell"
