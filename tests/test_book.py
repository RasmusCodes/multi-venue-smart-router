import random

from smartrouter.book import BUY, DARKBG, IOC, LP, NOISE, ROUTER, SELL, Book, DarkBook, Order


def mk(oid, side, price, qty, owner=LP):
    return Order(oid, owner, side, price, qty)


def test_price_then_time_priority():
    b = Book()
    b.add(mk(1, SELL, 101, 100))
    b.add(mk(2, SELL, 100, 100))
    b.add(mk(3, SELL, 100, 200))
    fills = []
    left = b.match(BUY, 101, 350, fills)
    assert left == 0
    assert [(o.oid, p, q) for o, p, q in fills] == [(2, 100, 100), (3, 100, 200), (1, 101, 50)]
    assert b.best_ask() == 101 and b.qty_at(SELL, 101) == 50


def test_limit_price_respected_and_remainder_returned():
    b = Book()
    b.add(mk(1, BUY, 99, 100))
    b.add(mk(2, BUY, 98, 100))
    fills = []
    left = b.match(SELL, 99, 300, fills)
    assert left == 200 and len(fills) == 1 and b.best_bid() == 98


def test_partial_fill_keeps_priority():
    b = Book()
    b.add(mk(1, BUY, 100, 300))
    b.add(mk(2, BUY, 100, 100))
    b.match(SELL, 100, 100, [])
    assert b.orders[1].qty == 200
    fills = []
    b.match(SELL, 100, 250, fills)
    assert [(o.oid, q) for o, _, q in fills] == [(1, 200), (2, 50)]


def test_cancel_and_empty_levels():
    b = Book()
    b.add(mk(1, BUY, 100, 100))
    b.add(mk(2, BUY, 100, 100))
    o, q = b.cancel(1)
    assert o.oid == 1 and q == 100 and b.qty_at(BUY, 100) == 100
    assert b.cancel(1) == (None, 0)
    b.cancel(2)
    assert b.best_bid() is None and 100 not in b.bids


def test_queue_ahead():
    b = Book()
    orders = [mk(i, SELL, 100, 100 * i) for i in range(1, 5)]
    for o in orders:
        b.add(o)
    b.cancel(2)
    assert b.queue_ahead(orders[3]) == 100 + 300
    assert b.queue_ahead(orders[0]) == 0


class RefBook:
    """Naive reference: a flat list, re-sorted on every match."""

    def __init__(self):
        self.orders = []
        self.seq = 0

    def add(self, oid, side, price, qty):
        self.seq += 1
        self.orders.append([oid, side, price, qty, self.seq])

    def cancel(self, oid):
        for o in self.orders:
            if o[0] == oid and o[3] > 0:
                q = o[3]
                o[3] = 0
                return q
        return 0

    def match(self, side, limit, qty):
        contra = [o for o in self.orders if o[1] == -side and o[3] > 0 and (o[2] <= limit if side == BUY else o[2] >= limit)]
        contra.sort(key=lambda o: (o[2] * side, o[4]))
        fills = []
        for o in contra:
            if qty == 0:
                break
            q = min(qty, o[3])
            o[3] -= q
            qty -= q
            fills.append((o[0], o[2], q))
        return qty, fills

    def levels(self, side, n):
        agg = {}
        for o in self.orders:
            if o[1] == side and o[3] > 0:
                agg[o[2]] = agg.get(o[2], 0) + o[3]
        px = sorted(agg, reverse=(side == BUY))[:n]
        return [(p, agg[p]) for p in px]


def test_differential_against_naive_reference():
    rng = random.Random(11)
    for trial in range(30):
        b, ref = Book(keep_ns=10**15), RefBook()
        live = []
        oid = 0
        for step in range(400):
            b.now = step
            u = rng.random()
            if u < 0.5:
                oid += 1
                side = BUY if rng.random() < 0.5 else SELL
                price = 100 + rng.randint(-5, 5)
                qty = 100 * rng.randint(1, 5)
                fills = []
                left = b.match(side, price, qty, fills)
                left_r, fills_r = ref.match(side, price, qty)
                assert left == left_r
                assert [(o.oid, p, q) for o, p, q in fills] == fills_r
                if left:
                    b.add(Order(oid, LP, side, price, left))
                    ref.add(oid, side, price, left)
                    live.append(oid)
            elif live:
                victim = rng.choice(live)
                _, q = b.cancel(victim)
                assert q == ref.cancel(victim)
            assert b.levels(BUY, 10) == ref.levels(BUY, 10)
            assert b.levels(SELL, 10) == ref.levels(SELL, 10)
            bb, ba = b.best_bid(), b.best_ask()
            assert bb is None or ba is None or bb < ba  # never crossed


def test_snapshot_rollback_reproduces_past_books():
    rng = random.Random(5)
    b = Book(keep_ns=10**12)
    history = []
    oid = 0
    live = []
    for t in range(1, 600):
        b.now = t
        if rng.random() < 0.6 or not live:
            oid += 1
            side = BUY if rng.random() < 0.5 else SELL
            price = (99 - rng.randint(0, 4)) if side == BUY else (101 + rng.randint(0, 4))
            if rng.random() < 0.15:  # marketable
                b.match(side, price + 3 * side, 100 * rng.randint(1, 4), [])
            else:
                b.add(Order(oid, LP, side, price, 100 * rng.randint(1, 4)))
                live.append(oid)
        else:
            b.cancel(live.pop(rng.randrange(len(live))))
        history.append((t, b.levels(BUY, 5), b.levels(SELL, 5)))
    for (t, bids, asks) in history[-300:]:
        got_b, got_a = b.snapshot_at(t, 5)
        assert got_b == bids and got_a == asks, t


def test_dark_book_fifo_and_limits():
    d = DarkBook()
    a = Order(1, DARKBG, SELL, None, 300)
    c = Order(2, DARKBG, SELL, None, 200, limit=10_050)  # will not sell below 10_050
    e = Order(3, DARKBG, SELL, None, 500)
    for o in (a, c, e):
        d.add(o)
    fills = []
    left = d.match_mid(BUY, 600, 10_000, None, fills)
    assert left == 0
    assert [(o.oid, q) for o, _, q in fills] == [(1, 300), (3, 300)]
    assert d.resting(SELL) == 200 + 200
    # the skipped order keeps its place ahead of the partly filled one
    fills = []
    d.match_mid(BUY, 1000, 10_050, None, fills)
    assert [(o.oid, q) for o, _, q in fills] == [(2, 200), (3, 200)]
    # a taker limit below the midpoint trades nothing
    d.add(Order(4, DARKBG, SELL, None, 100))
    assert d.match_mid(BUY, 100, 10_100, 10_000, []) == 100
