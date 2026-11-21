"""Training-only exploration: what happens to an order resting at the NBBO?

The explorer posts orders at random venues (``single``) or on every lit venue
at once (``joint``), keeps each for the wait a strategy would use, then
cancels. For each post it records

* ``exec``  - volume that traded at the post's price level on that venue while
              it rested (trades hit the front of the queue, so this is the
              outflow a longer queue would have absorbed);
* ``cf``    - the share of the queue ahead of the post that cancelled;
* ``Q0``    - the queue ahead when the post arrived;
* ``dm``    - side * (post price - mid at the end of the wait): what a fill
              at the post's price cost against the price that prevailed
              afterwards (negative = captured spread, positive = adverse
              selection);
* ``hsT``   - side * (far touch - mid) at the end of the wait: what crossing
              the spread then costs, before fees;
* ``dmid``  - side * (mid at posting - mid at the end): the market's drift
              over the wait, against the order's side.

All prices are the router's own delayed view. Measuring costs against the
mid at the END of the wait removes the market's drift, which every routing
choice shares, from venue comparisons: the parent's shortfall equals the sum
of these end-of-wait costs plus a drift term that does not depend on routing.

It reads the venue's book directly to measure these, which a live router
cannot do; that is why it only runs on training seeds, and its output is the
only thing strategies learn from.
"""

from __future__ import annotations

from ..book import BUY, DAY, SELL
from ..config import NS_PER_S
from ..detmath import Stream
from .policies import can_post, join_price


class _Probe:
    __slots__ = ("vi", "price", "side", "coid", "oid", "start", "end", "ahead", "q0", "exec", "canc", "mid0")


class Explorer:
    def __init__(self, name="explore", mode="single", wait_s=11.25, gap_s=0.5, sizes=(300, 600, 1000)):
        self.name = name
        self.mode = mode
        self.wait_ns = int(wait_s * NS_PER_S)
        self.gap_ns = int(gap_s * NS_PER_S)
        self.sizes = sizes
        self.rows_single = []  # (vi, exec, cf, q0, dm, hsT, dmid)
        self.rows_joint = []  # {"v": {vi: [exec, cf, q0]}, "dm": .., "hsT": .., "dmid": ..}
        self.trace = None

    def start(self, ctx, parent):
        self.ctx = ctx
        self.m = ctx.m
        self.rng = Stream(ctx.m.seed, "explorer")
        self.end_all = parent.t0 + parent.horizon_ns
        self.live = {}  # coid -> probe
        self.by_oid = {}
        self.m.fill_observers.append(self._on_fill)
        self.m.cancel_observers.append(self._on_cancel)
        self.m.rest_observers.append(self._on_rest)
        ctx.timer(ctx.now, ("probe",))

    # privileged observers -------------------------------------------------

    def _on_rest(self, v, o):
        pr = self.live.get(o.ref)
        if pr is None:
            return
        pr.oid = o.oid
        pr.start = self.m.t
        lvl = (v.book.bids if o.side == BUY else v.book.asks).get(o.price)
        ahead, q0 = set(), 0
        if lvl is not None:
            for x in lvl.orders:
                if x is o:
                    break
                if x.qty > 0:
                    ahead.add(x.oid)
                    q0 += x.qty
        pr.ahead = ahead
        pr.q0 = q0

    def _on_fill(self, v, taker, maker, px, q):
        for pr in self.live.values():
            if pr.start is not None and v.idx == pr.vi and px == pr.price and maker.side == pr.side:
                pr.exec += q

    def _on_cancel(self, v, o, q):
        for pr in self.live.values():
            if pr.start is not None and v.idx == pr.vi and o.oid in pr.ahead:
                pr.canc += q

    # probing -----------------------------------------------------------------

    def on_timer(self, ctx, tag):
        if tag[0] == "probe":
            if ctx.now + self.wait_ns > self.end_all:
                return
            self._launch()
        elif tag[0] == "end":
            self._finish(tag[1])

    def _launch(self):
        ctx = self.ctx
        r = self.rng.random
        side = BUY if r() < 0.5 else SELL
        snaps = ctx.snapshots()
        price = join_price(snaps, side)
        bb, ba = ctx.nbbo_of(snaps)
        if price is None or bb is None or ba is None or ba <= bb:
            ctx.timer(ctx.now + self.gap_ns, ("probe",))
            return
        mid0 = (bb + ba) / 2.0
        if self.mode == "single":
            vi = ctx.lit[int(r() * len(ctx.lit)) % len(ctx.lit)]
            venues = [vi] if can_post(snaps[vi], side, price) else []
        else:
            venues = [vi for vi in ctx.lit if can_post(snaps[vi], side, price)]
        if not venues:
            ctx.timer(ctx.now + self.gap_ns, ("probe",))
            return
        group = []
        for vi in venues:
            q = self.sizes[int(r() * len(self.sizes)) % len(self.sizes)]
            # same order flags as the router's real posts (venue protection applies where offered)
            coid = ctx.send(vi, side, q, price, DAY, kind="post", post_only=True, peg=True)
            pr = _Probe()
            pr.vi, pr.price, pr.side, pr.coid = vi, price, side, coid
            pr.oid, pr.start, pr.ahead, pr.q0, pr.exec, pr.canc, pr.mid0 = None, None, set(), 0, 0, 0, mid0
            pr.end = ctx.now + self.wait_ns
            self.live[coid] = pr
            group.append(coid)
        ctx.timer(ctx.now + self.wait_ns, ("end", tuple(group)))

    def _finish(self, group):
        ctx = self.ctx
        snaps = ctx.snapshots()
        bb, ba = ctx.nbbo_of(snaps)
        probes = [self.live.pop(c) for c in group if c in self.live]
        for pr in probes:
            ctx.cancel(pr.coid, reason="end")
        if probes and bb is not None and ba is not None and ba > bb:
            side = probes[0].side
            mid_t = (bb + ba) / 2.0
            far = ba if side == BUY else bb
            hs_t = side * (far - mid_t)
            dm = side * (probes[0].price - mid_t)
            dmid = side * (probes[0].mid0 - mid_t)
            per = {}
            for pr in probes:
                if pr.start is None:
                    continue  # rejected (post-only would cross on arrival)
                cf = (pr.canc / pr.q0) if pr.q0 > 0 else 0.0
                if cf > 1.0:
                    cf = 1.0
                per[pr.vi] = [pr.exec, cf, pr.q0]
                self.rows_single.append((pr.vi, pr.exec, cf, pr.q0, dm, hs_t, dmid))
            if self.mode == "joint" and per:
                self.rows_joint.append({"v": per, "dm": dm, "hsT": hs_t, "dmid": dmid})
        ctx.timer(ctx.now + self.gap_ns, ("probe",))

    def on_report(self, ctx, rep, ch):
        pass
