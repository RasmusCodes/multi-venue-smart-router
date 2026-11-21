"""Working a parent order: schedule, slice tactic, venue choice, bookkeeping.

Every strategy shares the same schedule: the parent is cut into equal slices
(TWAP). What differs is the tactic per slice and where child orders go:

* ``split="take"`` - cross the spread for the slice at once (take policy);
* ``split="post"`` - rest the slice at the NBBO on one venue (post policy),
  re-peg it if the market moves away, and cross the spread for whatever is
  left when the wait ends;
* ``split="ck"``   - Cont-Kukanov split of the slice between a market order
  and limit orders on several venues, then the same clean-up.

Any quantity still open when the horizon ends is taken with an escalating
limit price. The executor never has more shares filled-or-working than the
parent quantity (no over-fill), and works at most ``ahead_slices`` slices
ahead of the schedule.
"""

from __future__ import annotations

from ..book import BUY, DAY, IOC
from ..config import NS_PER_S, TICK
from ..detmath import Stream
from .ck import ck_allocate, take_cost_curve
from .policies import PostModel, Thompson, can_post, join_price, plan_take, queue_ahead_seen

LOT = 100


class Executor:
    def __init__(self, name, take="nbbo", post=None, split="take", wait_frac=0.75, dark_ping=False,
                 repeg_ms=500, slip_ticks=1, stats=None, ahead_slices=1.0, ck_scenarios=150, lam_o_scale=1.0,
                 max_escal=6, trace=False):
        self.name = name
        self.take_mode = take
        self.post_mode = post
        self.split = split
        self.wait_frac = wait_frac
        self.dark_ping = dark_ping
        self.repeg_ns = int(repeg_ms * 1_000_000)
        self.slip_ticks = slip_ticks
        self.stats = stats or {}
        self.ahead_slices = ahead_slices
        self.ck_scenarios = ck_scenarios
        self.lam_o_scale = lam_o_scale
        self.max_escal = max_escal
        self.trace = [] if trace else None
        self.survival = {int(k): float(v) for k, v in self.stats.get("survival", {}).items()}
        self.post_model = PostModel(self.stats) if (post == "queue" or split == "ck") else None
        joint = self.stats.get("joint", [])
        self.joint = joint[:ck_scenarios] if ck_scenarios else joint
        self.cleanup_fee = float(self.stats.get("cleanup_fee", 15.0))

    # ---------------------------------------------------------------- lifecycle

    def start(self, ctx, parent):
        self.ctx = ctx
        self.p = parent
        self.side = parent.side
        self.Q = parent.qty
        self.t0 = parent.t0
        self.H = parent.horizon_ns
        self.N = parent.slices
        self.L = self.H // self.N
        self.targets = [(self.Q * (i + 1) // self.N) // LOT * LOT for i in range(self.N)]
        self.targets[-1] = self.Q
        self.filled = 0
        self.take_need = 0
        self.post_need = 0
        self.wave = set()
        self.wave_filled = 0
        self.escal = 0
        self.dark_pending = None
        self.pinged = False
        self.posts = set()
        self.slice = -1
        self.deadline = self.t0
        self.final = False
        self.done_t = None
        self.rng = Stream(ctx.m.seed, "router:" + self.name)
        self.bandit = Thompson(ctx.lit, self.rng) if self.post_mode == "adaptive" else None
        self.n_ck = 0
        for i in range(self.N):
            ctx.timer(self.t0 + i * self.L, ("slice", i))
        ctx.timer(self.t0 + self.H, ("final",))
        if self.post_mode or self.split == "ck":
            ctx.timer(self.t0 + self.repeg_ns, ("repeg",))

    def note(self, kind, **kw):
        if self.trace is not None:
            kw["t"] = self.ctx.now
            kw["kind"] = kind
            self.trace.append(kw)

    # ---------------------------------------------------------------- bookkeeping

    def open_leaves(self) -> int:
        tot = 0
        ch = self.ctx.children
        for coid in self.wave:
            tot += ch[coid].leaves
        for coid in self.posts:
            tot += ch[coid].leaves
        if self.dark_pending is not None:
            tot += ch[self.dark_pending].leaves
        return tot

    def committed(self) -> int:
        return self.take_need + self.post_need + self.open_leaves()

    def headroom(self) -> int:
        return self.Q - self.filled - self.open_leaves()

    def add_take(self, q):
        if q <= 0:
            return
        if self.take_need <= 0 and not self.wave and self.dark_pending is None:
            self.pinged = False  # a new take job may ping the dark pool once
        self.take_need += q
        self.kick_take()

    # ---------------------------------------------------------------- timers

    def on_timer(self, ctx, tag):
        kind = tag[0]
        if kind == "slice":
            self.on_slice(tag[1])
        elif kind == "expire":
            if tag[1] == self.slice and not self.final:
                self.expire_posts()
        elif kind == "final":
            self.final = True
            self.expire_posts()
        elif kind == "retry":
            self.kick_take()
        elif kind == "repeg":
            self.on_repeg()
            if not self.final:
                ctx.timer(ctx.now + self.repeg_ns, ("repeg",))

    def on_slice(self, i):
        if self.final:
            return
        self.slice = i
        self.deadline = self.t0 + i * self.L + int(self.wait_frac * self.L)
        self.pinged = False
        need = self.targets[i] - self.filled - self.committed()
        if need <= 0:
            return
        if self.split == "take":
            self.note("slice", i=i, need=need, plan="take")
            self.add_take(need)
            return
        if self.split == "ck" and self.joint:
            self.ck_slice(i, need)
        else:
            self.note("slice", i=i, need=need, plan="post")
            self.post_need += need
            self.do_posts()
        self.ctx.timer(self.deadline, ("expire", i))

    def expire_posts(self):
        for coid in list(self.posts):
            self.ctx.cancel(coid, reason="expire")
        self.post_need = 0
        self.take_gap()

    def take_gap(self):
        """Cross the spread only for what the schedule still needs (not for deliberate over-posting)."""
        target = self.Q if self.final else self.targets[self.slice]
        gap = target - self.filled - self.committed()
        if gap > 0:
            self.add_take(gap)

    # ---------------------------------------------------------------- taking

    def kick_take(self):
        ctx = self.ctx
        if self.wave or self.dark_pending is not None or self.take_need <= 0:
            return
        room = self.headroom() - self.post_need
        q = min(self.take_need, room)
        if q <= 0:
            self.take_need = 0
            return
        if self.dark_ping and ctx.dark and not self.pinged:
            self.pinged = True
            snaps = ctx.snapshots()
            bb, ba = ctx.nbbo_of(snaps)
            far = ba if self.side == BUY else bb
            if far is not None:
                vi = ctx.dark[0]
                coid = ctx.send(vi, self.side, q, None, IOC, kind="dark", limit=far)
                self.dark_pending = coid
                self.take_need -= q
                self.note("dark", venue=vi, qty=q, limit=far)
                return
        plan = plan_take(ctx, self.side, q, room, self.take_mode, escal=self.escal, slip_ticks=self.slip_ticks,
                         survival=self.survival if self.take_mode == "model" else None)
        if not plan:
            self.escal = min(self.escal + 1, self.max_escal)
            ctx.timer(ctx.now + 5_000_000, ("retry",))
            return
        self.wave_filled = 0
        for (vi, qty, px, delay, seen) in plan:
            coid = ctx.send(vi, self.side, qty, px, IOC, kind="take", delay_ns=delay, snap_qty=seen)
            self.wave.add(coid)
            self.take_need -= qty
        self.note("wave", children=[(vi, qty, px, delay) for (vi, qty, px, delay, _) in plan], escal=self.escal)

    def wave_done(self):
        if self.take_need < 0:
            self.take_need = 0
        if self.wave_filled > 0:
            self.escal = 0
        else:
            self.escal = min(self.escal + 1, self.max_escal)
        if self.take_need > 0:
            self.kick_take()

    # ---------------------------------------------------------------- posting

    def post_venue(self, snaps, price, q):
        ctx = self.ctx
        allowed = [vi for vi in ctx.lit if can_post(snaps[vi], self.side, price)]
        if not allowed:
            return None
        mode = self.post_mode or "queue"
        if mode == "primary":
            return ctx.lit[0] if ctx.lit[0] in allowed else allowed[0]
        if mode == "rebate":
            return min(allowed, key=lambda vi: (ctx.venues[vi]["make_fee"], vi))
        if mode == "adaptive":
            return self.bandit.choose(allowed)
        # queue-aware: lowest expected cost per share from the fitted post model
        best, best_v = None, None
        for vi in allowed:
            if not self.post_model or not self.post_model.has(vi):
                continue
            qa = queue_ahead_seen(snaps[vi], self.side, price)
            c = self.post_model.cost(vi, q, qa, ctx.venues[vi]["make_fee"])
            if best is None or c < best:
                best, best_v = c, vi
        return best_v if best_v is not None else allowed[0]

    def joint_drift_mean(self):
        if getattr(self, "_d_bar", None) is None:
            tot = 0.0
            for row in self.joint:
                tot += row["dmid"]
            self._d_bar = tot / len(self.joint) if self.joint else 0.0
        return self._d_bar

    def future_cost(self):
        """Expected cost per share of executing a share later (best venue at its typical queue)."""
        if getattr(self, "_v_future", None) is None:
            pm = self.post_model
            best = None
            q = max(LOT, self.Q // self.N)
            for vi in self.ctx.lit:
                if pm is None or not pm.has(vi):
                    continue
                c = pm.cost(vi, q, pm.typical_queue(vi), self.ctx.venues[vi]["make_fee"])
                if best is None or c < best:
                    best = c
            self._v_future = best if best is not None else 0.0
        return self._v_future

    def send_post(self, vi, q, price, snaps, mid):
        ctx = self.ctx
        qa = queue_ahead_seen(snaps[vi], self.side, price)
        coid = ctx.send(vi, self.side, q, price, DAY, kind="post", post_only=True, peg=True, snap_qty=qa)
        ctx.children[coid].info = {"mid0": mid, "slice": self.slice}
        self.posts.add(coid)
        self.note("post", venue=vi, qty=q, price=price, ahead=qa)
        return coid

    def do_posts(self):
        ctx = self.ctx
        if self.post_need <= 0 or self.final:
            return
        if ctx.now >= self.deadline:
            self.post_need = 0
            self.take_gap()
            return
        snaps = ctx.snapshots()
        price = join_price(snaps, self.side)
        if price is None:
            return
        q = min(self.post_need, self.headroom())
        if q <= 0:
            self.post_need = 0
            return
        vi = self.post_venue(snaps, price, q)
        if vi is None:
            return  # every venue would cross; try again at the next re-peg check
        bb, ba = ctx.nbbo_of(snaps)
        mid = (bb + ba) / 2.0 if (bb is not None and ba is not None) else float(price)
        self.post_need -= q
        self.send_post(vi, q, price, snaps, mid)

    def on_repeg(self):
        ctx = self.ctx
        if self.final:
            return
        if self.posts:
            snaps = ctx.snapshots()
            price = join_price(snaps, self.side)
            if price is not None:
                for coid in list(self.posts):
                    ch = ctx.children[coid]
                    if ch.cancel_sent or not ch.open:
                        continue
                    behind = ch.price < price if self.side == BUY else ch.price > price
                    if behind:
                        ctx.cancel(coid, reason="repeg")
        if self.post_need > 0:
            self.do_posts()

    def ck_slice(self, i, need):
        ctx = self.ctx
        snaps = ctx.snapshots()
        price = join_price(snaps, self.side)
        bb, ba = ctx.nbbo_of(snaps)
        if price is None or bb is None or ba is None or ba <= bb:
            self.note("slice", i=i, need=need, plan="take (no two-sided NBBO)")
            self.add_take(need)
            return
        mid = (bb + ba) / 2.0
        hs = (ba - bb) / 2.0
        S = need
        ahead_cap = int(self.ahead_slices * (self.Q // self.N))
        cap = min(self.headroom() - self.post_need - self.take_need, S + ahead_cap)
        cap = cap // LOT * LOT
        if cap < LOT:
            self.add_take(need)
            return
        venues = [vi for vi in ctx.lit if can_post(snaps[vi], self.side, price)]
        if not venues:
            self.add_take(need)
            return
        qa = [queue_ahead_seen(snaps[vi], self.side, price) for vi in venues]
        fees = [ctx.venues[vi]["make_fee"] for vi in venues]
        excess, under, c_post = [], [], []
        # moment-match the scenario set: the market's drift has mean zero, so re-centre the sample drift
        # (otherwise its sampling error would tilt the market-order-versus-limit-order trade-off)
        d_bar = self.joint_drift_mean()
        for row in self.joint:
            per = row["v"]
            ex = []
            for vi, q_ahead in zip(venues, qa):
                s = per.get(str(vi)) or per.get(vi)
                if s is None:
                    ex.append(0)
                    continue
                e = s[0] - (1.0 - s[1]) * q_ahead
                ex.append(int(e // LOT * LOT) if e > 0 else 0)
            excess.append(ex)
            c_post.append([row["dm"] - d_bar + f for f in fees])
            under.append(row["hsT"] + self.cleanup_fee)
        take_cum = take_cost_curve(snaps, self.side, ctx.venues, mid, S)
        # an over-filled share replaces a later one: reward it by what a later share is expected to cost
        lam_o = -self.lam_o_scale * self.future_cost()
        M, Ls, exp_cost = ck_allocate(S, cap, c_post, excess, under, take_cum, lam_o, m_drift=0.0)
        self.n_ck += 1
        self.note("ck", i=i, need=need, M=M, L={venues[k]: Ls[k] for k in range(len(venues))},
                  exp_cost=exp_cost)
        for k, vi in enumerate(venues):
            if Ls[k] > 0:
                q = min(Ls[k], self.headroom())
                if q > 0:
                    self.send_post(vi, q, price, snaps, mid)
        if M > 0:
            self.add_take(M)

    # ---------------------------------------------------------------- reports

    def on_report(self, ctx, rep, ch):
        if rep.kind == "fill":
            self.filled += rep.qty
            if ch.coid in self.wave:
                self.wave_filled += rep.qty
            if self.filled >= self.Q and self.done_t is None:
                self.done_t = ctx.now
        if ch.open:
            return
        coid = ch.coid
        if ch.kind == "take":
            if coid in self.wave:
                self.wave.discard(coid)
                self.take_need += ch.canceled
                if not self.wave:
                    self.wave_done()
        elif ch.kind == "dark":
            if coid == self.dark_pending:
                self.dark_pending = None
                self.take_need += ch.canceled
                self.kick_take()
        elif ch.kind == "post":
            if coid not in self.posts:
                return
            self.posts.discard(coid)
            if self.bandit is not None:
                self.learn(ch)
            left = ch.canceled
            if left <= 0:
                return
            if self.final or ch.cancel_reason == "expire" or ctx.now >= self.deadline:
                self.take_gap()
            elif ch.cancel_reason == "repeg":
                self.post_need += left
                self.do_posts()
            else:  # venue protection or post-only reject: re-post at the next check
                self.post_need += left

    def learn(self, ch):
        """Realised cost per share of a finished post, against the mid now (the end of its wait)."""
        ctx = self.ctx
        if ch.qty <= 0:
            return
        bb, ba = ctx.nbbo()
        if bb is None or ba is None or ba <= bb:
            return
        side = self.side
        mid_t = (bb + ba) / 2.0
        cost = 0.0
        if ch.filled:
            cost += side * (ch.notional - mid_t * ch.filled) + ch.fees
        left = ch.qty - ch.filled
        if left > 0:
            far = ba if side == BUY else bb
            cost += left * (side * (far - mid_t) + self.cleanup_fee)
        self.bandit.update(ch.vi, cost / ch.qty)


def describe(ex: Executor) -> str:
    parts = [f"split={ex.split}", f"take={ex.take_mode}"]
    if ex.post_mode:
        parts.append(f"post={ex.post_mode}")
    if ex.dark_ping:
        parts.append("dark ping")
    return ", ".join(parts)


__all__ = ["Executor", "describe", "NS_PER_S"]
