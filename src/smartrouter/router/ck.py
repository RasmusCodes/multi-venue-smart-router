"""Optimal split between a market order and limit orders on several venues.

Follows the order-placement problem of Cont and Kukanov ("Optimal order
placement in limit order markets", Quantitative Finance, 2017). To buy S
shares within a horizon, choose a market order M and limit orders L_k at the
best bid of venues k = 1..K, behind Q_k shares already queued there. If xi_k
is the queue outflow at venue k over the horizon (trades plus cancellations
ahead of us), the limit order fills min(L_k, (xi_k - Q_k)^+) and the cost is

    V(X, xi) = take_cost(M) + sum_k c_k * fill_k
               + lambda_u * (S - A)^+ + lambda_o * (A - S)^+,   A = M + sum_k fill_k.

Cont and Kukanov use fixed per-share prices (half spread, fees, rebates) and
solve the expected-cost problem by stochastic approximation. Here the
expectation is a sample average over joint scenarios observed on training
seeds (sample-average approximation), and every price is measured against the
mid at the END of the horizon, scenario by scenario:

* c_k^n      = side * (limit price - end mid) + maker fee_k: a fill just before
               the price runs through the order costs more (adverse selection);
* lambda_u^n = end half spread + taker fee: crossing for the shortfall at the end;
* the market order pays the current take cost plus the expected drift;
* lambda_o   = minus the expected cost of executing a share later: an
               over-filled share replaces a later one.

The objective is convex in X (fills are concave non-decreasing in L_k and the
shortfall/overfill penalty is convex non-increasing then non-decreasing in A
as long as lambda_o >= -lambda_u), so steepest descent over lot-sized moves
reaches the lattice optimum in practice; a unit test checks it against brute
force on small instances.
"""

from __future__ import annotations

from ..book import BUY
from ..config import TICK


def take_cost_curve(snaps, side, venues, mid, max_shares, lot=100):
    """Cumulative cost (pu, vs mid, fees included) of taking 0..max_shares now, in lots.

    Walks the consolidated displayed book in all-in price order; beyond the
    displayed depth every further lot pays one more tick than the last level.
    """
    levels = []
    for vi, (bids, asks) in snaps.items():
        fee = venues[vi]["take_fee"]
        for (p, q) in (asks if side == BUY else bids):
            levels.append((side * p + fee, side * (p - mid) + fee, q))
    levels.sort(key=lambda x: x[0])
    n_lots = max_shares // lot
    cum = [0.0] * (n_lots + 1)
    li, left = 0, (levels[0][2] if levels else 0)
    last = (levels[-1][1] if levels else 50.0)
    for m in range(1, n_lots + 1):
        need = lot
        cost = 0.0
        while need > 0:
            if li >= len(levels):
                cost += need * (last + TICK)
                need = 0
                break
            take = left if left < need else need
            cost += take * levels[li][1]
            need -= take
            left -= take
            if left == 0:
                li += 1
                left = levels[li][2] if li < len(levels) else 0
        cum[m] = cum[m - 1] + cost
    return cum


def ck_allocate(S, cap, c_post, excess, under, take_cum, lam_o, m_drift=0.0, lot=100, max_iter=500):
    """Minimise the sample-average Cont-Kukanov cost.

    S        target shares (multiple of ``lot``)
    cap      maximum M + sum(L) (parent head-room), multiple of ``lot``
    c_post   c_post[n][k]: cost of a filled passive share at venue k in scenario n
    excess   excess[n][k]: shares a limit order at venue k would fill in scenario n
    under    under[n]: per-share cost of the shortfall in scenario n
    take_cum cumulative take cost by lots (see take_cost_curve)
    lam_o    per-share penalty (negative: reward) for filling more than S
    m_drift  expected per-share drift cost of executing the market order now
    Returns (M, [L_k]) in shares and the expected cost per share of the target.
    """
    N = len(excess)
    K = len(excess[0]) if N else 0
    if N == 0 or K == 0:
        return S, [0] * K, None
    max_m = min(S, (len(take_cum) - 1) * lot) // lot
    cap_lots = cap // lot

    def mcost(m):
        return take_cum[m] + m * lot * m_drift

    m = max_m  # start from a pure market order
    L = [0] * K
    A = [m * lot] * N
    fill = [[0] * K for _ in range(N)]

    def g(n, a):
        d = S - a
        return under[n] * d if d > 0 else -lam_o * d

    pen = [g(n, A[n]) for n in range(N)]

    def eval_L(k, newL, dm):
        """Objective change if L_k becomes newL and M changes by dm lots; also returns new fills."""
        tot = 0.0
        newf = [0] * N
        for n in range(N):
            e = excess[n][k]
            f = newL if newL < e else e
            newf[n] = f
            df = f - fill[n][k]
            a = A[n] + df + dm * lot
            tot += c_post[n][k] * df + g(n, a) - pen[n]
        return tot / N, newf

    def eval_M(dm):
        tot = 0.0
        for n in range(N):
            tot += g(n, A[n] + dm * lot) - pen[n]
        return tot / N

    for _ in range(max_iter):
        used = m + sum(L) // lot
        best = 0.0
        move = None
        if m > 0:
            d = mcost(m - 1) - mcost(m) + eval_M(-1)
            if d < best - 1e-9:
                best, move = d, ("M", -1, None, None)
        if m < max_m and used < cap_lots:
            d = mcost(m + 1) - mcost(m) + eval_M(+1)
            if d < best - 1e-9:
                best, move = d, ("M", +1, None, None)
        for k in range(K):
            if used < cap_lots:
                d, nf = eval_L(k, L[k] + lot, 0)
                if d < best - 1e-9:
                    best, move = d, ("L", k, +1, nf)
            if L[k] > 0:
                d, nf = eval_L(k, L[k] - lot, 0)
                if d < best - 1e-9:
                    best, move = d, ("L", k, -1, nf)
            if m > 0:
                d, nf = eval_L(k, L[k] + lot, -1)
                d += mcost(m - 1) - mcost(m)
                if d < best - 1e-9:
                    best, move = d, ("S", k, -1, nf)
        if move is None:
            break
        kind = move[0]
        if kind == "M":
            dm = move[1]
            m += dm
            for n in range(N):
                A[n] += dm * lot
        else:
            k, nf = move[1], move[3]
            if kind == "L":
                L[k] += move[2] * lot
                shift = 0
            else:  # shift one lot from the market order to venue k
                m -= 1
                L[k] += lot
                shift = lot
            for n in range(N):
                A[n] += nf[n] - fill[n][k] - shift
                fill[n][k] = nf[n]
        pen = [g(n, A[n]) for n in range(N)]
    acc = 0.0
    for n in range(N):
        s = pen[n]
        for k in range(K):
            s += c_post[n][k] * fill[n][k]
        acc += s
    total = mcost(m) + acc / N
    return m * lot, L, (total / S if S else None)


def ck_objective(M, L, S, c_post, excess, under, take_cum, lam_o, m_drift=0.0, lot=100):
    """Direct evaluation of the sample-average objective (used by tests)."""
    N = len(excess)
    acc = 0.0
    for n in range(N):
        a = M
        s = 0.0
        for k in range(len(L)):
            f = L[k] if L[k] < excess[n][k] else excess[n][k]
            a += f
            s += c_post[n][k] * f
        d = S - a
        s += under[n] * d if d > 0 else -lam_o * d
        acc += s
    return take_cum[M // lot] + M * m_drift + acc / N
