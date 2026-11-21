"""Discrete-event simulation of a fragmented market.

One stock trades on several venues. A latent fair value moves with news jumps
and with the order flow of takers (permanent impact). Background agents:

* liquidity providers (LPs) on each lit venue post and cancel limit orders
  around the touch they are willing to quote, which depends on fair value and
  on the venue's fees; they react to fair-value changes after a short delay;
* noise takers send marketable orders, routed fee-aware or by displayed size,
  and sometimes ping the dark pool first;
* latency arbitrageurs race to pick off quotes made stale by news;
* resting midpoint orders populate the dark pool.

The router under test talks to venues through ``RouterContext`` with realistic
delays: its orders and cancels take the venue's latency (plus jitter) to arrive,
execution reports take as long to come back, and its market data is the
venue's book as it was ``feed latency`` ago.

Random numbers come from named streams (see ``detmath``) so that runs of
different strategies on the same seed share the same exogenous history
(common random numbers).
"""

from __future__ import annotations

import heapq
import math
from bisect import bisect_right

from .book import BUY, DARKBG, DAY, IOC, LP, NOISE, ROUTER, SELL, SNIPER, Order
from .config import NS_PER_S, TICK, Scenario
from .detmath import Stream, cumulative, exp_from_uniform, hash_uniform, pick
from .venue import Venue

(EV_JUMP, EV_LP_ARRIVE, EV_LP_CANCEL, EV_LP_UPDATE, EV_NOISE, EV_SNIPE, EV_DARK_ARRIVE, EV_DARK_CANCEL,
 EV_ORDER, EV_CANCEL, EV_REPORT, EV_TIMER, EV_RELEASE, EV_PROTECT, EV_START, EV_DARK_SNIPE, EV_FADE) = range(17)

HUGE = 1 << 60

# hash_uniform stream keys (aligned draws)
_K_REACT_EXO, _K_REACT_IMP, _K_SNIPE, _K_LPLIFE, _K_DSNIPE, _K_FADE = 1, 2, 3, 4, 5, 6


class Report:
    """An execution report on its way from a venue to the router."""

    __slots__ = ("kind", "coid", "venue", "qty", "price", "liq", "fee", "t_venue", "reason")

    def __init__(self, kind, coid, venue, qty=0, price=None, liq=None, fee=0, t_venue=0, reason=""):
        self.kind = kind  # ack | fill | done | canceled | reject | cancel_reject
        self.coid = coid
        self.venue = venue
        self.qty = qty
        self.price = price
        self.liq = liq  # "M" maker / "T" taker
        self.fee = fee  # pu per share
        self.t_venue = t_venue
        self.reason = reason


class Parent:
    __slots__ = ("side", "qty", "t0", "horizon_ns", "slices", "arrival_mid", "arrival_fair", "end_ns")

    def __init__(self, side, qty, t0, horizon_ns, slices, arrival_mid, arrival_fair):
        self.side = side
        self.qty = qty
        self.t0 = t0
        self.horizon_ns = horizon_ns
        self.slices = slices
        self.arrival_mid = arrival_mid
        self.arrival_fair = arrival_fair
        self.end_ns = t0 + horizon_ns


class Market:
    def __init__(self, scenario: Scenario, seed: int):
        self.sc = scenario
        self.mc = mc = scenario.market
        self.seed = int(seed)
        self.t = 0
        self.heap = []
        self.seq = 0
        self._next_oid = 1
        keep = 2_000_000 + max(int(v.feed_latency_us * 1000) for v in scenario.venues) + \
            max(int(v.ref_latency_us * 1000) for v in scenario.venues)
        self.venues = [Venue(i, cfg, keep) for i, cfg in enumerate(scenario.venues)]
        self.lit = [v for v in self.venues if v.lit]
        self.dark = [v for v in self.venues if not v.lit]
        self.lit_books = [v.book for v in self.lit]
        self.F = float(mc.f0)
        self.f_t = [0]
        self.f_v = [self.F]
        self.jump_index = 0
        self.impact_index = 0
        self.fade_index = 0
        self.faded_qty = 0
        self.jumps = []  # (t, size) of every news jump: identical across strategies on one seed
        # NBBO history (used for the dark pool's delayed reference price)
        self.nb_t = [0]
        self.nb_mid = [None]
        # streams
        self.s_fv = Stream(seed, "fair-value")
        self.s_noise = Stream(seed, "noise")
        self.s_net = Stream(seed, "network")
        self.s_parent = Stream(seed, "parent")
        self.s_lp = {v.idx: Stream(seed, "lp:" + v.name) for v in self.lit}
        self.s_dark = {v.idx: Stream(seed, "dark:" + v.name) for v in self.dark}
        # cumulative distributions
        self.c_jump = cumulative(mc.jump_probs)
        self.c_noise = cumulative(mc.noise_probs)
        self.c_lvl = cumulative(mc.lp_levels)
        self.c_lpsize = cumulative(mc.lp_size_probs)
        self.c_dsize = cumulative(mc.dark_size_probs)
        # router plumbing
        self.router = None
        self.ctx = None
        self.parent = None
        self.parent_cfg = None
        self.rfills = []  # (t, venue, side, price, qty, liq, fee)
        self.coid_to_order = {}
        self.last_in = {v.idx: 0 for v in self.venues}  # FIFO arrival per venue, router -> venue
        self.last_out = {v.idx: 0 for v in self.venues}  # FIFO arrival per venue, venue -> router
        self.observers = []  # callables(market, event_kind, payload) for tracing / calibration
        self.fill_observers = []  # callables(venue, taker, maker, price, qty)
        self.cancel_observers = []  # callables(venue, order, qty)
        self.rest_observers = []  # callables(venue, router order) when a router order starts resting
        # statistics
        self.volume_by_owner = {}
        self.n_events = 0
        self.handlers = [
            self._on_jump, self._on_lp_arrive, self._on_lp_cancel, self._on_lp_update, self._on_noise,
            self._on_snipe, self._on_dark_arrive, self._on_dark_cancel, self._on_router_order,
            self._on_router_cancel, self._on_report, self._on_timer, self._on_release, self._on_protect,
            self._on_start, self._on_dark_snipe, self._on_fade,
        ]
        self._init_background()

    # ---------------------------------------------------------------- plumbing

    def oid(self) -> int:
        self._next_oid += 1
        return self._next_oid

    def push(self, t, kind, a=None, b=None):
        self.seq += 1
        heapq.heappush(self.heap, (t, self.seq, kind, a, b))

    def run(self, until_ns: int):
        heap = self.heap
        pop = heapq.heappop
        handlers = self.handlers
        books = self.lit_books
        n = 0
        while heap and heap[0][0] <= until_ns:
            t, _, kind, a, b = pop(heap)
            if t != self.t:
                self.t = t
                for bk in books:
                    bk.now = t
            handlers[kind](a, b)
            self._nbbo_update()
            n += 1
        self.n_events += n
        if until_ns > self.t:
            self.t = until_ns
            for bk in books:
                bk.now = until_ns

    # ---------------------------------------------------------------- state queries

    def nbbo(self):
        """Current (true, undelayed) national best bid and offer over lit venues."""
        bb = ba = None
        for v in self.lit:
            bk = v.book
            if bk.bid_px:
                p = bk.bid_px[-1]
                if bb is None or p > bb:
                    bb = p
            if bk.ask_px:
                p = bk.ask_px[0]
                if ba is None or p < ba:
                    ba = p
        return bb, ba

    def mid(self):
        bb, ba = self.nbbo()
        if bb is None or ba is None or bb >= ba:
            return None
        return (bb + ba) // 2

    def fair_at(self, t) -> float:
        return self.f_v[bisect_right(self.f_t, t) - 1]

    def digest(self) -> str:
        """FNV-1a fingerprint of the run: router fills, fair-value path and event count."""
        h = 0xCBF29CE484222325
        mask = 0xFFFFFFFFFFFFFFFF

        def mix(h, x):
            for b in repr(x).encode():
                h ^= b
                h = (h * 0x100000001B3) & mask
            return h

        for f in self.rfills:
            h = mix(h, f[:7])
        for t, v in zip(self.f_t, self.f_v):
            h = mix(h, (t, v))
        h = mix(h, self.n_events)
        return f"{h:016x}"

    def _nbbo_update(self):
        bb, ba = self.nbbo()
        mid = None if (bb is None or ba is None or bb >= ba) else (bb + ba) // 2
        if mid != self.nb_mid[-1]:
            self.nb_t.append(self.t)
            self.nb_mid.append(mid)
            if len(self.nb_t) > 4096:
                keep = bisect_right(self.nb_t, self.t - 50_000_000) - 1
                if keep > 0:
                    del self.nb_t[:keep]
                    del self.nb_mid[:keep]

    def ref_mid(self, v: Venue):
        """The dark pool's reference midpoint: the NBBO midpoint as of ``ref latency`` ago."""
        i = bisect_right(self.nb_t, self.t - v.ref_ns) - 1
        return self.nb_mid[i] if i >= 0 else None

    # ---------------------------------------------------------------- fair value

    def _move_fair(self, d_fair: float, exogenous: bool):
        self.F += d_fair
        self.f_t.append(self.t)
        self.f_v.append(self.F)
        if exogenous:
            self.jump_index += 1
            key, idx = _K_REACT_EXO, self.jump_index
        else:
            self.impact_index += 1
            key, idx = _K_REACT_IMP, self.impact_index
        for v in self.lit:
            target = v.valid_touch(self.F)
            if target != v.lp_target:
                v.lp_target = target
                v.lp_target_seq += 1
                u = hash_uniform(self.seed, key, idx, v.idx)
                delay = v.react_lo + int(u * (v.react_hi - v.react_lo + 1))
                self.push(self.t + delay, EV_LP_UPDATE, v.idx, (target[0], target[1], v.lp_target_seq))

    # ---------------------------------------------------------------- background set-up

    def _init_background(self):
        mc = self.mc
        if mc.jump_rate > 0:
            self.push(int(self.s_fv.exp(NS_PER_S / mc.jump_rate)), EV_JUMP)
        if mc.noise_rate > 0:
            self.push(int(self.s_noise.exp(NS_PER_S / mc.noise_rate)), EV_NOISE)
        for v in self.lit:
            v.lp_bid, v.lp_ask = v.valid_touch(self.F)
            v.lp_target = (v.lp_bid, v.lp_ask)
            if v.lp_rate > 0:
                self.push(int(self.s_lp[v.idx].exp(NS_PER_S / (2.0 * v.lp_rate))), EV_LP_ARRIVE, v.idx)
        for v in self.dark:
            if v.cfg.dark_rate > 0:
                self.push(int(self.s_dark[v.idx].exp(NS_PER_S / (2.0 * v.cfg.dark_rate))), EV_DARK_ARRIVE, v.idx)

    # ---------------------------------------------------------------- order entry at a lit venue

    def _arrive_lit(self, v: Venue, o: Order):
        """An order reaches a lit venue: apply the speed bump to marketable orders, then process."""
        if v.bump_ns and not o.post_only and v.book.crosses(o.side, o.price):
            self.push(self.t + v.bump_ns, EV_RELEASE, v.idx, o)
            return
        self._process_lit(v, o)

    def _process_lit(self, v: Venue, o: Order):
        bk = v.book
        o.ts = self.t
        if bk.crosses(o.side, o.price):
            if o.post_only:
                if o.owner == ROUTER:
                    self._report(v, Report("reject", o.ref, v.idx, qty=o.qty, t_venue=self.t, reason="post-only"))
                return
            fills = []
            o.qty = bk.match(o.side, o.price, o.qty, fills)
            if fills:
                self._on_fills(v, o, fills)
        if o.tif == IOC or o.qty == 0:
            if o.owner == ROUTER:
                self._report(v, Report("done", o.ref, v.idx, qty=o.qty, t_venue=self.t))
            o.qty = 0
            return
        bk.add(o)
        if o.owner == ROUTER:
            for obs in self.rest_observers:
                obs(v, o)
            self._report(v, Report("ack", o.ref, v.idx, qty=o.qty, price=o.price, t_venue=self.t))
        elif o.owner == LP and o.ref:
            # LP orders carry their pre-drawn lifetime in ``ref`` (keeps the draw aligned across strategies)
            self.push(self.t + o.ref, EV_LP_CANCEL, v.idx, o.oid)

    def _on_fills(self, v: Venue, taker: Order, fills):
        total = 0
        t = self.t
        prints = v.prints
        vbo = self.volume_by_owner
        for (mk, px, q) in fills:
            total += q
            prints.append((t, px, q, taker.side))
            v.volume += q
            key = (taker.owner, mk.owner)
            vbo[key] = vbo.get(key, 0) + q
            if mk.owner == ROUTER:
                self._router_fill(v, mk, px, q, "M")
            for obs in self.fill_observers:
                obs(v, taker, mk, px, q)
        taker.filled += total
        if len(prints) > 20000:
            for _ in range(10000):
                prints.popleft()
        if taker.owner == ROUTER:
            for (mk, px, q) in fills:
                self._router_fill(v, taker, px, q, "T")
        if v.lit and total and (taker.owner == NOISE or taker.owner == ROUTER):
            if self.mc.kappa:
                self._move_fair(taker.side * self.mc.kappa * total, exogenous=False)
            if total >= self.mc.fade_min_qty:
                # fast LPs on other venues see the print and pull quotes on the side being taken
                self.fade_index += 1
                worst = fills[-1][1]
                for w in self.lit:
                    if w is v or w.cfg.lp_fade <= 0:
                        continue
                    u = hash_uniform(self.seed, _K_FADE, self.fade_index, w.idx)
                    delay = w.react_lo + int(u * (w.react_hi - w.react_lo + 1))
                    self.push(self.t + delay, EV_FADE, w.idx, (taker.side, worst))

    # ---------------------------------------------------------------- background agents

    def _on_jump(self, _a, _b):
        mc = self.mc
        r = self.s_fv.random
        u_size, u_sign, u_next = r(), r(), r()
        self.push(self.t + int(exp_from_uniform(u_next, NS_PER_S / mc.jump_rate)), EV_JUMP)
        size = mc.jump_sizes[pick(self.c_jump, u_size)]
        d = size if u_sign < 0.5 else -size
        self.jumps.append((self.t, d))
        self._move_fair(float(d), exogenous=True)
        new_fair = self.F
        j = self.jump_index
        # latency arbitrageurs race for quotes the news made stale
        for v in self.lit:
            if d > 0:
                limit = math.floor((new_fair - v.take_fee - mc.sniper_edge) / TICK) * TICK
                qty = v.book.depth_through(SELL, limit)
                side = BUY
            else:
                limit = math.ceil((new_fair + v.take_fee + mc.sniper_edge) / TICK) * TICK
                qty = v.book.depth_through(BUY, limit)
                side = SELL
            if qty > 0:
                u = hash_uniform(self.seed, _K_SNIPE, j, v.idx)
                lat = v.snipe_lo + int(u * (v.snipe_hi - v.snipe_lo + 1))
                self.push(self.t + lat, EV_SNIPE, v.idx, (side, limit, min(qty, mc.sniper_max_qty)))
            if v.protect_ns:
                self.push(self.t + v.protect_ns, EV_PROTECT, v.idx, (d, new_fair))
        if abs(d) >= mc.dark_snipe_min_jump:
            for v in self.dark:
                side = BUY if d > 0 else SELL
                resting = v.book.resting(-side)
                mid = self.ref_mid(v)
                if resting <= 0 or mid is None:
                    continue
                if side == BUY:
                    lim = new_fair - v.take_fee - mc.sniper_edge
                    ok = mid <= lim
                else:
                    lim = new_fair + v.take_fee + mc.sniper_edge
                    ok = mid >= lim
                if ok:
                    u = hash_uniform(self.seed, _K_DSNIPE, j, v.idx)
                    lat = v.snipe_lo + int(u * (v.snipe_hi - v.snipe_lo + 1))
                    self.push(self.t + lat, EV_DARK_SNIPE, v.idx, (side, lim, min(resting, mc.sniper_max_qty)))

    def _on_snipe(self, vi, payload):
        side, limit, qty = payload
        v = self.venues[vi]
        o = Order(self.oid(), SNIPER, side, limit, qty, tif=IOC, venue=vi)
        self._arrive_lit(v, o)

    def _on_dark_snipe(self, vi, payload):
        side, limit, qty = payload
        v = self.venues[vi]
        o = Order(self.oid(), SNIPER, side, None, qty, tif=IOC, venue=vi)
        self._dark_take(v, o, limit)

    def _on_release(self, vi, o):
        self._process_lit(self.venues[vi], o)

    def _on_protect(self, vi, payload):
        """Venue-side protection: withdraw router pegs that the news just made stale."""
        d, new_fair = payload
        v = self.venues[vi]
        bk = v.book
        edge = self.mc.sniper_edge + v.take_fee
        if d < 0:
            lo = math.ceil((new_fair + edge) / TICK) * TICK
            victims = [o for o in list(bk.orders.values()) if o.owner == ROUTER and o.peg and o.side == BUY
                       and o.price >= lo]
        else:
            hi = math.floor((new_fair - edge) / TICK) * TICK
            victims = [o for o in list(bk.orders.values()) if o.owner == ROUTER and o.peg and o.side == SELL
                       and o.price <= hi]
        for o in victims:
            _, q = bk.cancel(o.oid)
            if q:
                for obs in self.cancel_observers:
                    obs(v, o, q)
                self._report(v, Report("canceled", o.ref, v.idx, qty=q, t_venue=self.t, reason="protect"))

    def _on_fade(self, wi, payload):
        """LPs pull a share of their quotes that a taker of ``side`` could reach up to ``px``.

        Orders leave from the back of each queue until ``lp_fade`` of the LP
        quantity at that price is gone; the same quantity is re-posted one tick
        further away. Deterministic, so it needs no random draw.
        """
        side, px = payload
        w = self.venues[wi]
        bk = w.book
        phi = w.cfg.lp_fade
        if side == BUY:
            prices = [p for p in bk.ask_px if p <= px]
            levels, away = bk.asks, TICK
        else:
            prices = [p for p in bk.bid_px if p >= px]
            levels, away = bk.bids, -TICK
        for p in prices:
            lvl = levels.get(p)
            if lvl is None:
                continue
            lp_orders = [o for o in lvl.orders if o.qty > 0 and o.owner == LP]
            lp_qty = 0
            for o in lp_orders:
                lp_qty += o.qty
            target = int(lp_qty * phi)
            pulled = 0
            for o in reversed(lp_orders):
                if pulled >= target:
                    break
                _, q = bk.cancel(o.oid)
                pulled += q
                for obs in self.cancel_observers:
                    obs(w, o, q)
            if pulled:
                self.faded_qty += pulled
                self._lp_place(w, -side, p + away, pulled, hash_uniform(self.seed, _K_LPLIFE, wi, p, self.fade_index))

    def _lp_place(self, v: Venue, side, price, qty, life_u):
        if qty <= 0:
            return
        o = Order(self.oid(), LP, side, price, qty, venue=v.idx)
        o.ref = 1 + int(exp_from_uniform(life_u, v.lp_life_ns))
        self._arrive_lit(v, o)

    def _on_lp_arrive(self, vi, _b):
        v = self.venues[vi]
        r = self.s_lp[vi].random
        u_side, u_lvl, u_size, u_life, u_next = r(), r(), r(), r(), r()
        self.push(self.t + int(exp_from_uniform(u_next, NS_PER_S / (2.0 * v.lp_rate))), EV_LP_ARRIVE, vi)
        side = BUY if u_side < 0.5 else SELL
        k = pick(self.c_lvl, u_lvl)
        size = self.mc.lp_sizes[pick(self.c_lpsize, u_size)]
        if v.cfg.lp_size_mult != 1.0:
            size = max(100, int(size * v.cfg.lp_size_mult / 100) * 100)
        price = v.lp_bid - k * TICK if side == BUY else v.lp_ask + k * TICK
        self._lp_place(v, side, price, size, u_life)

    def _on_lp_cancel(self, vi, oid):
        v = self.venues[vi]
        o, q = v.book.cancel(oid)
        if o is not None and q:
            for obs in self.cancel_observers:
                obs(v, o, q)

    def _on_lp_update(self, vi, payload):
        b, a, seq = payload
        v = self.venues[vi]
        if seq <= v.lp_applied_seq:
            return
        v.lp_applied_seq = seq
        v.lp_bid, v.lp_ask = b, a
        bk = v.book
        mc = self.mc
        far = mc.lp_max_levels * TICK
        stale_b = bk.cancel_owner_range(BUY, b + 1, HUGE, LP)
        stale_a = bk.cancel_owner_range(SELL, 0, a - 1, LP)
        gone = stale_b + stale_a + bk.cancel_owner_range(BUY, 0, b - far - 1, LP) + \
            bk.cancel_owner_range(SELL, a + far + 1, HUGE, LP)
        if self.cancel_observers:
            for (o, q) in gone:
                for obs in self.cancel_observers:
                    obs(v, o, q)
        qb = 0
        for (_, q) in stale_b:
            qb += q
        qa = 0
        for (_, q) in stale_a:
            qa += q
        rb = int(qb * mc.lp_repost / 100) * 100
        ra = int(qa * mc.lp_repost / 100) * 100
        hu = hash_uniform
        if rb:
            self._lp_place(v, BUY, b, rb, hu(self.seed, _K_LPLIFE, vi, seq, 1))
        if ra:
            self._lp_place(v, SELL, a, ra, hu(self.seed, _K_LPLIFE, vi, seq, 2))
        bb = bk.best_bid()
        if (bb is None or bb < b) and mc.lp_improve_qty:
            self._lp_place(v, BUY, b, mc.lp_improve_qty, hu(self.seed, _K_LPLIFE, vi, seq, 3))
        ba = bk.best_ask()
        if (ba is None or ba > a) and mc.lp_improve_qty:
            self._lp_place(v, SELL, a, mc.lp_improve_qty, hu(self.seed, _K_LPLIFE, vi, seq, 4))

    def _on_noise(self, _a, _b):
        mc = self.mc
        r = self.s_noise.random
        u_side, u_size, u_mode, u_pick, u_dark, u_next = r(), r(), r(), r(), r(), r()
        self.push(self.t + int(exp_from_uniform(u_next, NS_PER_S / mc.noise_rate)), EV_NOISE)
        side = BUY if u_side < 0.5 else SELL
        qty = mc.noise_sizes[pick(self.c_noise, u_size)]
        if self.dark and u_dark < mc.noise_dark_prob:
            o = Order(self.oid(), NOISE, side, None, qty, tif=IOC, venue=self.dark[0].idx)
            self._dark_take(self.dark[0], o, None)
            qty = o.qty
            if qty <= 0:
                return
        self._sweep(NOISE, side, qty, u_mode < mc.noise_fee_aware, u_pick, mc.noise_max_ticks)

    def _sweep(self, owner, side, qty, fee_aware, u_pick, max_ticks):
        """Instant multi-venue sweep used by background takers."""
        bb, ba = self.nbbo()
        best = ba if side == BUY else bb
        if best is None:
            return
        rest = SELL if side == BUY else BUY
        limit = best + side * max_ticks * TICK
        plan = {}
        order = []
        remaining = qty
        p = best
        while remaining > 0 and (p <= limit if side == BUY else p >= limit):
            avail = [(v, v.book.qty_at(rest, p)) for v in self.lit]
            avail = [(v, q) for v, q in avail if q > 0]
            if avail:
                if fee_aware:
                    avail.sort(key=lambda x: (x[0].take_fee, x[0].idx))
                else:
                    tot = 0
                    for _, q in avail:
                        tot += q
                    target = u_pick * tot
                    acc = 0
                    first = 0
                    for i, (_, q) in enumerate(avail):
                        acc += q
                        if target < acc:
                            first = i
                            break
                    head = avail[first]
                    others = avail[:first] + avail[first + 1:]
                    others.sort(key=lambda x: (-x[1], x[0].idx))
                    avail = [head] + others
                for v, q in avail:
                    take = q if q < remaining else remaining
                    if v.idx not in plan:
                        plan[v.idx] = [0, p]
                        order.append(v)
                    plan[v.idx][0] += take
                    plan[v.idx][1] = p
                    remaining -= take
                    if remaining == 0:
                        break
            p += side * TICK
        for v in order:
            q, px = plan[v.idx]
            o = Order(self.oid(), owner, side, px, q, tif=IOC, venue=v.idx)
            self._arrive_lit(v, o)

    def _dark_take(self, v: Venue, o: Order, limit):
        """A midpoint IOC (any owner) trades against resting dark orders at the reference midpoint."""
        mid = self.ref_mid(v)
        if mid is None:
            return
        fills = []
        o.qty = v.book.match_mid(o.side, o.qty, mid, limit, fills)
        if fills:
            self._on_fills(v, o, fills)

    def _on_dark_arrive(self, vi, _b):
        v = self.venues[vi]
        r = self.s_dark[vi].random
        u_side, u_size, u_life, u_next = r(), r(), r(), r()
        self.push(self.t + int(exp_from_uniform(u_next, NS_PER_S / (2.0 * v.cfg.dark_rate))), EV_DARK_ARRIVE, vi)
        side = BUY if u_side < 0.5 else SELL
        size = self.mc.dark_sizes[pick(self.c_dsize, u_size)]
        if v.cfg.dark_size_mult != 1.0:
            size = max(100, int(size * v.cfg.dark_size_mult / 100) * 100)
        o = Order(self.oid(), DARKBG, side, None, size, venue=vi)
        self._dark_take(v, o, None)
        if o.qty > 0:
            o.ts = self.t
            v.book.add(o)
            self.push(self.t + int(exp_from_uniform(u_life, v.dark_life_ns)), EV_DARK_CANCEL, vi, o.oid)

    def _on_dark_cancel(self, vi, oid):
        self.venues[vi].book.cancel(oid)

    # ---------------------------------------------------------------- router plumbing

    def attach(self, strategy, parent_cfg, context_cls):
        """Attach the router under test; the parent order starts after the warm-up."""
        self.router = strategy
        self.parent_cfg = parent_cfg
        self.ctx = context_cls(self, strategy)
        self.push(int(self.mc.warmup_s * NS_PER_S), EV_START)

    def start_time(self) -> int:
        return int(self.mc.warmup_s * NS_PER_S)

    def end_time(self) -> int:
        pc = self.parent_cfg or self.sc.parent
        return self.start_time() + int((pc.horizon_s + pc.tail_s) * NS_PER_S)

    def _on_start(self, _a, _b):
        pc = self.parent_cfg
        u = self.s_parent.random()
        side = pc.side if pc.side in (BUY, SELL) else (BUY if u < 0.5 else SELL)
        mid = self.mid()
        if mid is None:
            bb, ba = self.nbbo()
            mid = bb if bb is not None else ba
        self.parent = Parent(side, pc.qty, self.t, int(pc.horizon_s * NS_PER_S), pc.slices, mid, self.F)
        self.router.start(self.ctx, self.parent)

    def _jitter(self, v: Venue) -> int:
        if v.jit_ns <= 0:
            return 0
        return int(self.s_net.random() * (v.jit_ns + 1))

    def router_send(self, coid, vi, side, qty, price, tif, post_only, peg, limit, delay_ns):
        v = self.venues[vi]
        arrive = self.t + delay_ns + v.lat_ns + self._jitter(v)
        if arrive < self.last_in[vi]:
            arrive = self.last_in[vi]
        self.last_in[vi] = arrive
        o = Order(self.oid(), ROUTER, side, price, qty, ref=coid, tif=tif, post_only=post_only, peg=peg,
                  limit=limit, venue=vi)
        self.coid_to_order[coid] = o
        self.push(arrive, EV_ORDER, vi, o)
        return arrive

    def router_cancel(self, coid, vi, delay_ns):
        v = self.venues[vi]
        arrive = self.t + delay_ns + v.lat_ns + self._jitter(v)
        if arrive < self.last_in[vi]:
            arrive = self.last_in[vi]
        self.last_in[vi] = arrive
        self.push(arrive, EV_CANCEL, vi, coid)

    def router_timer(self, at_ns, tag):
        self.push(max(at_ns, self.t), EV_TIMER, tag)

    def _report(self, v: Venue, rep: Report):
        arrive = self.t + v.lat_ns + self._jitter(v)
        if arrive < self.last_out[v.idx]:
            arrive = self.last_out[v.idx]
        self.last_out[v.idx] = arrive
        self.push(arrive, EV_REPORT, rep)

    def _router_fill(self, v: Venue, o: Order, px, q, liq):
        fee = v.make_fee if liq == "M" else v.take_fee
        self.rfills.append((self.t, v.idx, o.side, px, q, liq, fee, o.ref))
        self._report(v, Report("fill", o.ref, v.idx, qty=q, price=px, liq=liq, fee=fee, t_venue=self.t))

    def _on_router_order(self, vi, o):
        v = self.venues[vi]
        if v.lit:
            self._arrive_lit(v, o)
            return
        # dark venue: midpoint order (IOC ping or resting peg)
        o.ts = self.t
        self._dark_take(v, o, o.limit)
        if o.tif == IOC or o.qty == 0:
            self._report(v, Report("done", o.ref, vi, qty=o.qty, t_venue=self.t))
            o.qty = 0
            return
        v.book.add(o)
        self._report(v, Report("ack", o.ref, vi, qty=o.qty, t_venue=self.t))

    def _on_router_cancel(self, vi, coid):
        v = self.venues[vi]
        o = self.coid_to_order.get(coid)
        if o is None:
            return
        got, q = v.book.cancel(o.oid)
        if got is not None and q > 0:
            for obs in self.cancel_observers:
                obs(v, got, q)
            self._report(v, Report("canceled", coid, vi, qty=q, t_venue=self.t, reason="request"))
        else:
            self._report(v, Report("cancel_reject", coid, vi, t_venue=self.t))

    def _on_report(self, rep, _b):
        self.ctx.deliver(rep)

    def _on_timer(self, tag, _b):
        self.router.on_timer(self.ctx, tag)
