import itertools
import random

import pytest

from smartrouter.book import BUY, SELL
from smartrouter.calibrate import calibrate
from smartrouter.config import ParentConfig, preset
from smartrouter.episode import run_episode
from smartrouter.router import STRATEGIES
from smartrouter.router.ck import ck_allocate, ck_objective, take_cost_curve
from smartrouter.router.policies import PostModel, plan_take


@pytest.fixture(scope="module")
def tiny_stats():
    sc = preset("baseline")
    return calibrate(sc, n_single=4, n_joint=4, n_take=2, workers=1, log=lambda *_: None)


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_strategy_completes_without_overfill(name, tiny_stats, baseline_fast):
    parent = ParentConfig(side=0, qty=6_000, horizon_s=60.0, slices=6, tail_s=15.0)
    for seed in (1, 2):
        res = run_episode(baseline_fast, name, seed, stats=tiny_stats, parent=parent)
        assert res["filled"] == 6_000, (name, seed, res["filled"])
        assert res["unfilled"] == 0


class FakeCtx:
    def __init__(self, snaps, venues):
        self._snaps = snaps
        self.venues = venues
        self.lit = sorted(snaps)

    def snapshots(self):
        return self._snaps

    @staticmethod
    def nbbo_of(snaps):
        bb = max((b[0][0] for b, a in snaps.values() if b), default=None)
        ba = min((a[0][0] for b, a in snaps.values() if a), default=None)
        return bb, ba


def venues_info():
    # venue 0: fee 30, lat 150us; venue 1: fee -15 (inverted), lat 220us; venue 2: fee 10, lat 380us
    return [
        {"take_fee": 30, "make_fee": -20, "lat_ns": 150_000, "jit_ns": 0, "bump_ns": 0},
        {"take_fee": -15, "make_fee": 20, "lat_ns": 220_000, "jit_ns": 0, "bump_ns": 0},
        {"take_fee": 10, "make_fee": -10, "lat_ns": 380_000, "jit_ns": 0, "bump_ns": 0},
    ]


def test_naive_sweep_is_fee_blind_and_fee_sweep_is_not():
    snaps = {
        0: ([(10_000, 500)], [(10_100, 500), (10_200, 500)]),
        1: ([(10_000, 300)], [(10_100, 300)]),
        2: ([(10_000, 300)], [(10_100, 300)]),
    }
    ctx = FakeCtx(snaps, venues_info())
    naive = plan_take(ctx, BUY, 600, 10_000, "nbbo")
    assert [(v, q) for v, q, *_ in naive] == [(0, 500), (1, 100)]  # price, then venue index
    fee = plan_take(ctx, BUY, 600, 10_000, "fee")
    assert [(v, q) for v, q, *_ in fee] == [(1, 300), (2, 300)]  # cheapest all-in first


def test_synchronised_sweep_aligns_arrivals():
    snaps = {i: ([(10_000, 300)], [(10_100, 300)]) for i in range(3)}
    vi = venues_info()
    plan = plan_take(FakeCtx(snaps, vi), BUY, 900, 10_000, "sync")
    arrivals = {v: delay + vi[v]["lat_ns"] for v, _q, _p, delay, _s in plan}
    assert len(set(arrivals.values())) == 1
    assert min(p[3] for p in plan) == 0  # the slowest venue is sent first, without delay


def test_model_sweep_oversizes_only_by_survival():
    snaps = {0: ([], [(10_100, 1000)]), 1: ([], [(10_100, 1000)])}
    vi = venues_info()[:2]
    plan = plan_take(FakeCtx(snaps, vi), BUY, 500, 10_000, "model", survival={0: 0.5, 1: 0.5})
    assert sum(q for _v, q, *_ in plan) == 1000  # 500 needed at 50% survival
    plan = plan_take(FakeCtx(snaps, vi), BUY, 500, 600, "model", survival={0: 0.5, 1: 0.5})
    assert sum(q for _v, q, *_ in plan) == 600  # never beyond the parent's head-room


def test_sell_side_mirrors_buy_side():
    snaps = {0: ([(10_000, 400)], [(10_100, 400)]), 1: ([(10_000, 400)], [(10_100, 400)])}
    plan = plan_take(FakeCtx(snaps, venues_info()[:2]), SELL, 500, 10_000, "fee")
    assert plan[0][0] == 1 and plan[0][2] == 10_000


def brute_force(S, cap, c_post, excess, under, take_cum, lam_o, lot=100):
    K = len(excess[0])
    best = None
    for M in range(0, S + 1, lot):
        for L in itertools.product(range(0, cap + 1, lot), repeat=K):
            if M + sum(L) > cap:
                continue
            v = ck_objective(M, list(L), S, c_post, excess, under, take_cum, lam_o)
            if best is None or v < best[0] - 1e-9:
                best = (v, M, L)
    return best


def test_cont_kukanov_descent_reaches_brute_force_optimum():
    rng = random.Random(4)
    for trial in range(25):
        S, cap, K, N = 400, 700, 2, 30
        excess = [[100 * rng.randint(0, 6) for _ in range(K)] for _ in range(N)]
        c_post = [[rng.uniform(-80, 40) for _ in range(K)] for _ in range(N)]
        under = [rng.uniform(40, 200) for _ in range(N)]
        take_cum = [0.0]
        for m in range(1, S // 100 + 1):
            take_cum.append(take_cum[-1] + 100 * (60 + 10 * m))
        lam_o = rng.uniform(-20, 40)
        M, L, _ = ck_allocate(S, cap, c_post, excess, under, take_cum, lam_o)
        got = ck_objective(M, L, S, c_post, excess, under, take_cum, lam_o)
        best = brute_force(S, cap, c_post, excess, under, take_cum, lam_o)
        assert got <= best[0] + 1e-6 * abs(best[0]) + 1e-6, (trial, got, best)


def test_take_cost_curve_is_increasing_and_convex():
    snaps = {0: ([], [(10_100, 300), (10_200, 500)]), 1: ([], [(10_100, 200)])}
    cum = take_cost_curve(snaps, BUY, venues_info()[:2], 10_050, 2000)
    inc = [b - a for a, b in zip(cum, cum[1:])]
    assert all(x > 0 for x in inc)
    assert all(b >= a - 1e-9 for a, b in zip(inc, inc[1:]))


def _stats(rows):
    # rows: (exec, cancel_frac, q0, dm, hsT, dmid)
    return {"post": {"0": [list(r) for r in rows]}, "cleanup_fee": 20.0}


def test_post_model_formula_and_queue_effect():
    # benign samples: a fill earns the half spread (dm = -50), crossing later costs 50 + 20 fee
    pm = PostModel(_stats([(800, 0.5, 0, -50.0, 50.0, 0.0), (300, 0.0, 0, -50.0, 50.0, 0.0)]))
    # q=500, queue 0: fills 500 and 300 -> mean of (500*(-50-20) and 300*(-70) + 200*70) / 500
    expect = ((500 * -70.0) + (300 * -70.0 + 200 * 70.0)) / 2 / 500
    assert abs(pm.cost(0, 500, 0, -20) - expect) < 1e-9
    costs = [pm.cost(0, 500, q, -20) for q in (0, 200, 600, 2000)]
    assert all(b >= a for a, b in zip(costs, costs[1:]))  # no adverse selection: queue only hurts
    # adverse sample: the order fills only when the price runs through it (dm = +150 > crossing cost)
    adverse = PostModel(_stats([(900, 0.0, 0, 150.0, 50.0, 0.0)]))
    assert adverse.cost(0, 500, 800, -20) < adverse.cost(0, 500, 0, -20)  # standing back avoids the fill


def test_post_model_drift_recentring():
    # the same fills with a +40 drift in every sample: re-centring removes the drift's mean
    base = [(800, 0.5, 0, -50.0, 50.0, 0.0), (300, 0.0, 0, -50.0, 50.0, 0.0)]
    drifted = [(e, c, q, dm + 40.0, h, d + 40.0) for (e, c, q, dm, h, d) in base]
    a, b = PostModel(_stats(base)), PostModel(_stats(drifted))
    assert abs(a.cost(0, 500, 100, -20) - b.cost(0, 500, 100, -20)) < 1e-9
