from smartrouter.book import BUY, IOC, ROUTER, SELL
from smartrouter.config import NS_PER_S, ParentConfig, preset
from smartrouter.episode import run_episode
from smartrouter.market import Market
from smartrouter.router import RouterContext


class Scripted:
    """A tiny strategy that runs a list of (time offset ns, callable(ctx)) actions."""

    name = "scripted"

    def __init__(self, actions):
        self.actions = actions
        self.reports = []
        self.done_t = None

    def start(self, ctx, parent):
        self.ctx = ctx
        self.parent = parent
        for i, (dt, _) in enumerate(self.actions):
            ctx.timer(ctx.now + dt, ("act", i))

    def on_timer(self, ctx, tag):
        self.actions[tag[1]][1](ctx)

    def on_report(self, ctx, rep, ch):
        self.reports.append((ctx.now, rep.kind, rep.coid, rep.qty, rep.price, rep.liq, rep.fee, rep.reason))


def scripted_market(actions, scenario=None, seed=3, run_s=5.0):
    sc = scenario or preset("baseline")
    sc.market.warmup_s = 10.0
    m = Market(sc, seed)
    strat = Scripted(actions)
    m.attach(strat, ParentConfig(side=1, qty=1000, horizon_s=run_s, slices=1, tail_s=0.0), RouterContext)
    m.run(int((10.0 + run_s) * NS_PER_S))
    return m, strat


def test_same_seed_is_bit_identical(baseline_fast, short_parent):
    a = run_episode(baseline_fast, "sync", 11, parent=short_parent, return_market=True)[1].digest()
    b = run_episode(baseline_fast, "sync", 11, parent=short_parent, return_market=True)[1].digest()
    assert a == b


def test_news_is_shared_across_strategies(baseline_fast, short_parent):
    """Common random numbers: the exogenous jumps do not depend on what the router does."""
    jumps = []
    for s in ("nbbo", "post_primary", "primary"):
        _, m, _ = run_episode(baseline_fast, s, 21, parent=short_parent, return_market=True)
        jumps.append(m.jumps)
    assert jumps[0] == jumps[1] == jumps[2] and len(jumps[0]) > 5


def test_order_and_report_latency_and_fifo():
    sent = {}

    def send(ctx):
        snaps = ctx.snapshots()
        bb, ba = ctx.nbbo_of(snaps)
        sent["coid"] = ctx.send(1, BUY, 100, bb - 300, kind="post")  # deep bid on BRAVO: rests
        sent["t"] = ctx.now
        ctx.cancel(sent["coid"])  # sent immediately after: must arrive after the order

    m, strat = scripted_market([(1000, send)])
    v = m.venues[1]
    kinds = [r[1] for r in strat.reports]
    assert kinds == ["ack", "canceled"]
    t_ack = strat.reports[0][0]
    assert t_ack - sent["t"] >= 2 * v.lat_ns  # order out + report back


def test_router_sees_delayed_book():
    seen = {}

    def look(ctx):
        v = ctx.m.venues[0]
        seen["snap"] = ctx.snapshot(0)
        seen["true_then"] = v.book.snapshot_at(ctx.now - v.feed_ns)
        seen["now"] = (v.book.levels(BUY, 5), v.book.levels(SELL, 5))

    scripted_market([(3 * NS_PER_S, look)])
    assert seen["snap"] == seen["true_then"]


def test_taker_and_maker_fees_are_charged():
    def take(ctx):
        snaps = ctx.snapshots()
        bb, ba = ctx.nbbo_of(snaps)
        ctx.send(2, BUY, 100, ba + 200, tif=IOC, kind="take")  # CHARLIE: taker rebate

    m, strat = scripted_market([(2 * NS_PER_S, take)])
    fills = [r for r in strat.reports if r[1] == "fill"]
    assert fills, "expected a fill on the inverted venue"
    for r in fills:
        assert r[5] == "T" and r[6] == m.venues[2].take_fee == -15


def test_speed_bump_delays_marketable_orders():
    info = {}

    def take(ctx):
        bb, ba = ctx.nbbo()
        info["sent"] = ctx.now
        ctx.send(3, BUY, 100, ba + 500, tif=IOC, kind="take")  # DELTA has a 350 us bump

    m, strat = scripted_market([(2 * NS_PER_S, take)])
    v = m.venues[3]
    fill_times = [t for (t, vi, *_rest) in m.rfills if vi == 3]
    assert fill_times
    assert fill_times[0] - info["sent"] >= v.lat_ns + v.bump_ns


def test_volume_and_fills_conserved(baseline_fast, short_parent):
    res, m, strat = run_episode(baseline_fast, "nbbo", 5, parent=short_parent, return_market=True)
    assert res["filled"] == sum(q for (_t, _v, _s, _p, q, *_r) in m.rfills)
    assert res["filled"] == sum(ch.filled for ch in m.ctx.children.values())
    for v in m.lit:
        assert v.volume >= 0
    # every lit book is uncrossed at the end of the run
    for v in m.lit:
        bb, ba = v.book.best_bid(), v.book.best_ask()
        assert bb is None or ba is None or bb < ba


def test_background_market_stylised_facts():
    sc = preset("baseline")
    m = Market(sc, 2)
    spreads, n = {}, 0
    t = 0
    while t < 200 * NS_PER_S:
        t += NS_PER_S // 5
        m.run(t)
        if t < 60 * NS_PER_S:
            continue
        bb, ba = m.nbbo()
        if bb is not None and ba is not None:
            spreads[ba - bb] = spreads.get(ba - bb, 0) + 1
            n += 1
    assert spreads.get(100, 0) / n > 0.85  # tick-constrained: mostly one-tick spreads
    by_taker = {}
    for (tk, _mk), q in m.volume_by_owner.items():
        by_taker[tk] = by_taker.get(tk, 0) + q
    total = sum(by_taker.values())
    assert 0.05 < by_taker.get(2, 0) / total < 0.5  # latency-arbitrage share of volume
