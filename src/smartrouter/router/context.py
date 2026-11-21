"""What a router can see and do.

A strategy only interacts with the market through ``RouterContext``:

* ``snapshot(v)`` - venue v's top five levels per side as they were
  ``feed latency`` ago (never the live book);
* ``send`` / ``cancel`` - messages that take the venue's one-way latency plus
  jitter to arrive, in FIFO order per venue;
* ``timer`` - a wake-up call;
* execution reports, delivered after the return latency.

The context also keeps the router's own view of each child order, built only
from those reports.
"""

from __future__ import annotations

from ..book import BUY, DAY, IOC, SELL


class Child:
    __slots__ = ("coid", "vi", "side", "qty", "price", "tif", "kind", "sent", "arrive_est", "filled", "open",
                 "cancel_sent", "cancel_reason", "tag", "notional", "fees", "acked", "canceled", "snap_qty",
                 "info")

    def __init__(self, coid, vi, side, qty, price, tif, kind, sent, tag):
        self.coid = coid
        self.vi = vi
        self.side = side
        self.qty = qty
        self.price = price
        self.tif = tif
        self.kind = kind  # "take" | "post" | "dark"
        self.sent = sent
        self.arrive_est = sent
        self.filled = 0
        self.open = True
        self.cancel_sent = False
        self.cancel_reason = ""
        self.tag = tag
        self.notional = 0  # sum price * qty of fills
        self.fees = 0  # sum fee * qty of fills
        self.acked = False
        self.canceled = 0
        self.snap_qty = 0  # displayed quantity the router saw when it sent this child
        self.info = None  # strategy-specific payload

    @property
    def leaves(self) -> int:
        return self.qty - self.filled - self.canceled if self.open else 0


class RouterContext:
    def __init__(self, market, strategy):
        self.m = market
        self.strategy = strategy
        self.children = {}
        self._next = 0
        self.venues = [v.info() for v in market.venues]
        self.lit = [v.idx for v in market.venues if v.lit]
        self.dark = [v.idx for v in market.venues if not v.lit]
        self.n_sent = 0
        self.n_cancels = 0

    # ---------------------------------------------------------------- time and data

    @property
    def now(self) -> int:
        return self.m.t

    def snapshot(self, vi):
        """(bids, asks): lists of (price, qty), best first, as of now - feed latency."""
        v = self.m.venues[vi]
        return v.book.snapshot_at(self.m.t - v.feed_ns)

    def snapshots(self):
        return {vi: self.snapshot(vi) for vi in self.lit}

    @staticmethod
    def nbbo_of(snaps):
        bb = ba = None
        for bids, asks in snaps.values():
            if bids and (bb is None or bids[0][0] > bb):
                bb = bids[0][0]
            if asks and (ba is None or asks[0][0] < ba):
                ba = asks[0][0]
        return bb, ba

    def nbbo(self):
        return self.nbbo_of(self.snapshots())

    def prints_seen(self, vi, since_ns):
        """Trade prints of venue v visible to the router (published before now - feed latency)."""
        v = self.m.venues[vi]
        cutoff = self.m.t - v.feed_ns
        return [p for p in v.prints if since_ns <= p[0] <= cutoff]

    # ---------------------------------------------------------------- actions

    def send(self, vi, side, qty, price, tif=DAY, kind="take", post_only=False, peg=False, limit=None,
             delay_ns=0, tag=None, snap_qty=0):
        assert qty > 0 and side in (BUY, SELL)
        self._next += 1
        coid = self._next
        ch = Child(coid, vi, side, qty, price, tif, kind, self.m.t + delay_ns, tag)
        ch.snap_qty = snap_qty
        self.children[coid] = ch
        ch.arrive_est = self.m.router_send(coid, vi, side, qty, price, tif, post_only, peg, limit, delay_ns)
        self.n_sent += 1
        return coid

    def cancel(self, coid, reason="", delay_ns=0):
        ch = self.children[coid]
        if not ch.open:
            return False
        if ch.cancel_sent:
            if reason:
                ch.cancel_reason = reason
            return False
        ch.cancel_sent = True
        ch.cancel_reason = reason
        self.m.router_cancel(coid, ch.vi, delay_ns)
        self.n_cancels += 1
        return True

    def timer(self, at_ns, tag):
        self.m.router_timer(at_ns, tag)

    # ---------------------------------------------------------------- reports

    def deliver(self, rep):
        ch = self.children.get(rep.coid)
        if ch is None:
            return
        k = rep.kind
        if k == "fill":
            ch.filled += rep.qty
            ch.notional += rep.price * rep.qty
            ch.fees += rep.fee * rep.qty
        elif k == "ack":
            ch.acked = True
        elif k in ("done", "canceled", "reject"):
            ch.canceled += rep.qty
            if k != "canceled" or ch.qty - ch.filled - ch.canceled <= 0:
                ch.open = False
        elif k == "cancel_reject":
            pass
        if ch.open and ch.qty - ch.filled - ch.canceled <= 0:
            ch.open = False
        self.strategy.on_report(self, rep, ch)


__all__ = ["Child", "RouterContext", "BUY", "SELL", "DAY", "IOC"]
