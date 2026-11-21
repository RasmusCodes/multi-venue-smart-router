"""Order books.

``Book`` is a displayed limit order book with price-time priority: better
prices trade first and, within a price, orders trade in the order they arrived
(FIFO). Cancelled orders are marked dead (qty 0) and skipped lazily, so a
cancel is O(1) apart from removing an emptied price level.

Every change to a price level's displayed quantity is appended to a short
mutation log. Market data reaches the router with a delay, and
``Book.snapshot_at(T)`` rebuilds the top of the book as it was at time T by
rolling those mutations back, instead of storing a copy of the book after
every event.

``DarkBook`` holds non-displayed midpoint orders in FIFO order per side.
"""

from __future__ import annotations

from bisect import insort
from collections import deque

BUY = 1
SELL = -1

# order owners
LP = 0  # background liquidity providers
NOISE = 1  # uninformed takers
SNIPER = 2  # latency arbitrageurs
DARKBG = 3  # background resting dark-pool orders
ROUTER = 4  # the router under test
OWNER_NAMES = {LP: "lp", NOISE: "noise", SNIPER: "sniper", DARKBG: "dark", ROUTER: "router"}

DAY = 0
IOC = 1


class Order:
    __slots__ = ("oid", "owner", "side", "price", "qty", "orig", "ts", "ref", "tif", "post_only", "peg", "limit",
                 "venue", "filled")

    def __init__(self, oid, owner, side, price, qty, ts=0, ref=None, tif=DAY, post_only=False, peg=False,
                 limit=None, venue=-1):
        self.oid = oid
        self.owner = owner
        self.side = side
        self.price = price  # lit: limit price; dark: None (midpoint)
        self.qty = qty  # remaining live quantity
        self.orig = qty
        self.ts = ts
        self.ref = ref  # router client order id
        self.tif = tif
        self.post_only = post_only
        self.peg = peg  # router order that a protected venue may withdraw on news
        self.limit = limit  # dark orders: worst acceptable midpoint
        self.venue = venue
        self.filled = 0

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"Order({self.oid}, owner={self.owner}, side={self.side}, px={self.price}, qty={self.qty}/"
                f"{self.orig}, ts={self.ts})")


class Level:
    __slots__ = ("price", "orders", "total")

    def __init__(self, price):
        self.price = price
        self.orders = deque()
        self.total = 0


class Book:
    __slots__ = ("bids", "asks", "bid_px", "ask_px", "orders", "mlog", "now", "keep_ns")

    def __init__(self, keep_ns: int = 5_000_000):
        self.bids = {}
        self.asks = {}
        self.bid_px = []  # ascending; best bid is the last element
        self.ask_px = []  # ascending; best ask is the first element
        self.orders = {}  # live resting orders by id
        self.mlog = deque()  # (t, side, price, delta displayed qty)
        self.now = 0
        self.keep_ns = keep_ns

    # ---------------------------------------------------------------- queries

    def best_bid(self):
        return self.bid_px[-1] if self.bid_px else None

    def best_ask(self):
        return self.ask_px[0] if self.ask_px else None

    def qty_at(self, side, price) -> int:
        lvl = (self.bids if side == BUY else self.asks).get(price)
        return lvl.total if lvl else 0

    def depth_through(self, resting_side, limit) -> int:
        """Displayed quantity on ``resting_side`` at prices a taker with ``limit`` can reach."""
        total = 0
        if resting_side == SELL:
            for p in self.ask_px:
                if p > limit:
                    break
                total += self.asks[p].total
        else:
            for p in reversed(self.bid_px):
                if p < limit:
                    break
                total += self.bids[p].total
        return total

    def levels(self, side, n=5):
        if side == BUY:
            px = self.bid_px[::-1][:n]
            return [(p, self.bids[p].total) for p in px]
        return [(p, self.asks[p].total) for p in self.ask_px[:n]]

    def queue_ahead(self, order) -> int:
        """Live displayed quantity ahead of ``order`` at its price."""
        lvl = (self.bids if order.side == BUY else self.asks).get(order.price)
        if lvl is None:
            return 0
        ahead = 0
        for o in lvl.orders:
            if o is order:
                return ahead
            ahead += o.qty
        return ahead

    def crosses(self, side, price) -> bool:
        if side == BUY:
            return bool(self.ask_px) and self.ask_px[0] <= price
        return bool(self.bid_px) and self.bid_px[-1] >= price

    # ---------------------------------------------------------------- mutations

    def _log(self, side, price, delta):
        mlog = self.mlog
        mlog.append((self.now, side, price, delta))
        cutoff = self.now - self.keep_ns
        while mlog[0][0] < cutoff:
            mlog.popleft()

    def add(self, o: Order):
        """Rest an order at the back of its price level (caller guarantees it does not cross)."""
        if o.side == BUY:
            book, px = self.bids, self.bid_px
        else:
            book, px = self.asks, self.ask_px
        lvl = book.get(o.price)
        if lvl is None:
            lvl = Level(o.price)
            book[o.price] = lvl
            if o.side == BUY and (not px or o.price > px[-1]):
                px.append(o.price)
            elif o.side == SELL and (not px or o.price < px[0]):
                px.insert(0, o.price)
            else:
                insort(px, o.price)
        lvl.orders.append(o)
        lvl.total += o.qty
        self.orders[o.oid] = o
        self._log(o.side, o.price, o.qty)

    def cancel(self, oid):
        """Cancel a live order. Returns (order, cancelled qty) or (None, 0)."""
        o = self.orders.pop(oid, None)
        if o is None:
            return None, 0
        q = o.qty
        o.qty = 0
        book, px = (self.bids, self.bid_px) if o.side == BUY else (self.asks, self.ask_px)
        lvl = book[o.price]
        lvl.total -= q
        if lvl.total == 0:
            del book[o.price]
            px.remove(o.price)
        self._log(o.side, o.price, -q)
        return o, q

    def match(self, side, limit, qty, fills) -> int:
        """Match an incoming order of ``side`` up to price ``limit``.

        Appends (maker order, price, quantity) to ``fills`` and returns the
        unfilled quantity. Resting orders keep price-time priority.
        """
        if side == BUY:
            px, book, rest_side = self.ask_px, self.asks, SELL
            while qty > 0 and px:
                p = px[0]
                if p > limit:
                    break
                qty = self._eat(book[p], p, qty, fills, rest_side)
                if book[p].total == 0:
                    del book[p]
                    px.pop(0)
        else:
            px, book, rest_side = self.bid_px, self.bids, BUY
            while qty > 0 and px:
                p = px[-1]
                if p < limit:
                    break
                qty = self._eat(book[p], p, qty, fills, rest_side)
                if book[p].total == 0:
                    del book[p]
                    px.pop()
        return qty

    def _eat(self, lvl, p, qty, fills, rest_side):
        dq = lvl.orders
        orders = self.orders
        taken = 0
        while qty > 0 and dq:
            o = dq[0]
            if o.qty == 0:
                dq.popleft()
                continue
            q = o.qty if o.qty < qty else qty
            o.qty -= q
            o.filled += q
            qty -= q
            taken += q
            fills.append((o, p, q))
            if o.qty == 0:
                dq.popleft()
                del orders[o.oid]
        lvl.total -= taken
        if taken:
            self._log(rest_side, p, -taken)
        return qty

    def cancel_owner_range(self, side, lo, hi, owner):
        """Cancel every live order of ``owner`` on ``side`` priced in [lo, hi]. Returns the orders."""
        px = self.bid_px if side == BUY else self.ask_px
        book = self.bids if side == BUY else self.asks
        prices = [p for p in px if lo <= p <= hi]
        out = []
        for p in prices:
            lvl = book.get(p)
            if lvl is None:
                continue
            victims = [o for o in lvl.orders if o.qty > 0 and o.owner == owner]
            for o in victims:
                q = o.qty
                self.cancel(o.oid)
                out.append((o, q))
        return out

    # ---------------------------------------------------------------- delayed view

    def snapshot_at(self, t, depth=5):
        """Top ``depth`` levels per side as displayed at time ``t`` (t <= now, within keep_ns)."""
        adj_b, adj_a = {}, {}
        for (ts, side, price, delta) in reversed(self.mlog):
            if ts <= t:
                break
            if side == BUY:
                adj_b[price] = adj_b.get(price, 0) + delta
            else:
                adj_a[price] = adj_a.get(price, 0) + delta
        if not adj_b:
            bids = self.levels(BUY, depth)
        else:
            cand = {p: self.bids[p].total for p in self.bid_px[::-1][: depth + len(adj_b)]}
            for p, d in adj_b.items():
                lvl = self.bids.get(p)
                cand[p] = (lvl.total if lvl else 0) - d
            bids = sorted(((p, q) for p, q in cand.items() if q > 0), reverse=True)[:depth]
        if not adj_a:
            asks = self.levels(SELL, depth)
        else:
            cand = {p: self.asks[p].total for p in self.ask_px[: depth + len(adj_a)]}
            for p, d in adj_a.items():
                lvl = self.asks.get(p)
                cand[p] = (lvl.total if lvl else 0) - d
            asks = sorted((p, q) for p, q in cand.items() if q > 0)[:depth]
        return bids, asks


class DarkBook:
    """Non-displayed midpoint orders, FIFO per side."""

    __slots__ = ("buys", "sells", "orders", "total_buy", "total_sell")

    def __init__(self):
        self.buys = deque()
        self.sells = deque()
        self.orders = {}
        self.total_buy = 0
        self.total_sell = 0

    def add(self, o: Order):
        (self.buys if o.side == BUY else self.sells).append(o)
        self.orders[o.oid] = o
        if o.side == BUY:
            self.total_buy += o.qty
        else:
            self.total_sell += o.qty

    def cancel(self, oid):
        o = self.orders.pop(oid, None)
        if o is None:
            return None, 0
        q = o.qty
        o.qty = 0
        if o.side == BUY:
            self.total_buy -= q
        else:
            self.total_sell -= q
        return o, q

    def resting(self, side) -> int:
        return self.total_buy if side == BUY else self.total_sell

    def match_mid(self, side, qty, mid, limit, fills) -> int:
        """Match a taker of ``side`` at ``mid`` against resting contra orders (FIFO)."""
        if limit is not None and ((side == BUY and mid > limit) or (side == SELL and mid < limit)):
            return qty
        dq = self.sells if side == BUY else self.buys
        orders = self.orders
        kept = []
        taken = 0
        while qty > 0 and dq:
            o = dq.popleft()
            if o.qty == 0:
                continue
            if o.limit is not None and ((o.side == SELL and mid < o.limit) or (o.side == BUY and mid > o.limit)):
                kept.append(o)  # this resting order's limit excludes the current midpoint
                continue
            q = o.qty if o.qty < qty else qty
            o.qty -= q
            o.filled += q
            qty -= q
            taken += q
            fills.append((o, mid, q))
            if o.qty > 0:
                kept.append(o)
                break
            del orders[o.oid]
        for o in reversed(kept):  # restore priority of skipped / partially filled orders
            dq.appendleft(o)
        if side == BUY:
            self.total_sell -= taken
        else:
            self.total_buy -= taken
        return qty
