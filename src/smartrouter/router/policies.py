"""Venue-selection policies.

Take policies turn "buy q shares now" into one immediate-or-cancel (IOC) child
order per venue:

* ``primary``  - everything to the primary venue, walking its book;
* ``nbbo``     - displayed price only (fee-blind), all children sent at once,
                 ties go to the primary venue: the textbook naive router;
* ``fee``      - all-in price (price + taker fee or - taker rebate), sent at once;
* ``sync``     - all-in price, and send times staggered so every child reaches
                 its venue at the same moment (slow venues first);
* ``model``    - ``sync`` plus sizing by each venue's fitted probability that
                 displayed liquidity is still there on arrival.

Post policies choose where to rest a passive order at the NBBO:

* ``primary``  - the primary venue;
* ``rebate``   - the venue paying the highest maker rebate;
* ``queue``    - the venue with the lowest expected cost per share given the
                 queue ahead, the venue's fees, its fitted fill outcomes and
                 the cost of crossing the spread for whatever does not fill;
* ``adaptive`` - Thompson sampling on realised costs, learned online.
"""

from __future__ import annotations

import math

from ..book import BUY
from ..config import TICK


def far_side(snap, side):
    """The side of a venue snapshot a taker of ``side`` trades against."""
    bids, asks = snap
    return asks if side == BUY else bids


def near_side(snap, side):
    bids, asks = snap
    return bids if side == BUY else asks


def expected_latency(v) -> float:
    return v["lat_ns"] + v["jit_ns"] / 2.0


def plan_take(ctx, side, qty, headroom, mode, escal=0, slip_ticks=1, survival=None, venues=None):
    """Return [(venue, qty, limit price, send delay ns, displayed qty seen)] for one wave of IOCs."""
    snaps = ctx.snapshots()
    bb, ba = ctx.nbbo_of(snaps)
    best = ba if side == BUY else bb
    if best is None:
        return []
    limit = best + side * (slip_ticks + escal) * TICK
    vinfo = ctx.venues
    cands = venues if venues is not None else ctx.lit
    if mode == "primary":
        cands = [ctx.lit[0]]
    levels = []
    for vi in cands:
        fee = vinfo[vi]["take_fee"]
        lat = expected_latency(vinfo[vi])
        for (p, q) in far_side(snaps[vi], side):
            if (side == BUY and p > limit) or (side != BUY and p < limit):
                break
            if mode in ("nbbo", "primary"):
                key = (side * p, vi)
            else:
                key = (side * p + fee, lat, vi)
            levels.append((key, vi, p, q))
    levels.sort(key=lambda x: x[0])
    alloc = {}
    order = []
    remaining = float(qty)
    budget = headroom
    for _, vi, p, q in levels:
        if remaining <= 0 or budget <= 0:
            break
        if mode == "model" and survival:
            # ask for enough that the expected fill (request x survival) covers what is still needed
            s = max(0.05, survival.get(vi, 1.0))
            want = int(math.ceil(remaining / s / 100.0)) * 100
            req = min(q, budget, want)
            remaining -= req * s
        else:
            req = min(q, int(remaining), budget)
            remaining -= req
        if req <= 0:
            continue
        budget -= req
        if vi not in alloc:
            alloc[vi] = [0, p, 0]
            order.append(vi)
        a = alloc[vi]
        a[0] += req
        a[1] = p
        a[2] += q
    if not order:
        return []
    if mode in ("sync", "model"):
        lat = {vi: expected_latency(vinfo[vi]) + vinfo[vi]["bump_ns"] for vi in order}
        slowest = max(lat.values())
        delays = {vi: int(slowest - lat[vi]) for vi in order}
    else:
        delays = {vi: 0 for vi in order}
    return [(vi, alloc[vi][0], alloc[vi][1], delays[vi], alloc[vi][2]) for vi in order]


def join_price(snaps, side):
    """Price at which a passive order joins the NBBO on its own side."""
    bb, ba = None, None
    for bids, asks in snaps.values():
        if bids and (bb is None or bids[0][0] > bb):
            bb = bids[0][0]
        if asks and (ba is None or asks[0][0] < ba):
            ba = asks[0][0]
    if side == BUY:
        if bb is not None:
            return bb
        return ba - TICK if ba is not None else None
    if ba is not None:
        return ba
    return bb + TICK if bb is not None else None


def queue_ahead_seen(snap, side, price) -> int:
    for (p, q) in near_side(snap, side):
        if p == price:
            return q
        if (side == BUY and p < price) or (side != BUY and p > price):
            break
    return 0


def can_post(snap, side, price) -> bool:
    """A post-only order at ``price`` would not cross this venue's displayed book."""
    far = far_side(snap, side)
    if not far:
        return True
    best = far[0][0]
    return price < best if side == BUY else price > best


class PostModel:
    """Expected cost per share of resting on a venue, against the mid at the end of the wait.

    Built from exploration samples (training seeds only): for each past post on
    venue v we know the volume that traded at its price level during the wait
    (``exec``), the share of the queue ahead that cancelled (``cf``), and, from
    the router's own view at the end of the wait, ``dm`` = side * (post price -
    mid) and ``hsT`` = the half spread then. For a new order of q shares behind
    Q shares,

        filled = min(q, max(0, exec - (1 - cf) * Q))
        cost   = filled * (dm + maker fee) + (q - filled) * (hsT + taker fee)

    averaged over the venue's samples. A fill that came just before the price
    moved through the order has a large ``dm`` (adverse selection); a non-fill
    costs the spread crossing at the end. Measuring against the end-of-wait mid
    removes the market's drift, which every routing choice shares.
    """

    def __init__(self, stats):
        raw = {int(k): v for k, v in stats.get("post", {}).items()}
        self.take_fee = float(stats.get("cleanup_fee", 15.0))
        # Moment-match the market drift: each venue's samples are re-centred so their mean drift is zero.
        # Fair value is a martingale, so the drift's sample mean is pure noise, and it would differ between
        # venues by chance; the link between filling and the drift (adverse selection) is kept because it
        # sits in WHICH samples filled, not in the mean.
        mu = 0.0
        self.samples = {}
        for vi, rows in raw.items():
            if not rows:
                self.samples[vi] = []
                continue
            dv = 0.0
            for r in rows:
                dv += r[5]
            shift = dv / len(rows) - mu
            self.samples[vi] = [[r[0], r[1], r[2], r[3] - shift, r[4], r[5] - shift] for r in rows]

    def has(self, vi) -> bool:
        return bool(self.samples.get(vi))

    def cost(self, vi, q, queue_ahead, make_fee) -> float:
        rows = self.samples.get(vi)
        if not rows:
            return float("inf")
        tot = 0.0
        tf = self.take_fee
        for row in rows:
            ex, cf, dm, hs_t = row[0], row[1], row[3], row[4]
            f = ex - (1.0 - cf) * queue_ahead
            if f < 0:
                f = 0.0
            if f > q:
                f = float(q)
            tot += f * (dm + make_fee) + (q - f) * (hs_t + tf)
        return tot / (len(rows) * q)

    def typical_queue(self, vi) -> float:
        rows = self.samples.get(vi) or []
        if not rows:
            return 0.0
        tot = 0.0
        for row in rows:
            tot += row[2]
        return tot / len(rows)


class Thompson:
    """Gaussian Thompson sampling over venues on realised cost per share (pu)."""

    def __init__(self, venues, rng, prior_mean=0.0, prior_sd=40.0, noise_sd=60.0):
        self.mu = {v: prior_mean for v in venues}
        self.prec = {v: 1.0 / (prior_sd * prior_sd) for v in venues}
        self.noise_prec = 1.0 / (noise_sd * noise_sd)
        self.rng = rng

    def choose(self, allowed):
        best, best_v = None, None
        for v in allowed:
            sd = math.sqrt(1.0 / self.prec[v])
            draw = self.mu[v] + sd * self.rng.normal()
            if best is None or draw < best:
                best, best_v = draw, v
        return best_v

    def update(self, v, cost, weight=1.0):
        p0 = self.prec[v]
        p1 = p0 + weight * self.noise_prec
        self.mu[v] = (self.mu[v] * p0 + cost * weight * self.noise_prec) / p1
        self.prec[v] = p1
