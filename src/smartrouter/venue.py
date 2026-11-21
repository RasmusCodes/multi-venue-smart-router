"""A trading venue: order book, fee schedule, latency profile and LP state."""

from __future__ import annotations

import math
from collections import deque

from .book import Book, DarkBook
from .config import NS_PER_S, NS_PER_US, TICK, VenueConfig


class Venue:
    def __init__(self, idx: int, cfg: VenueConfig, keep_ns: int):
        self.idx = idx
        self.cfg = cfg
        self.name = cfg.name
        self.lit = cfg.kind == "lit"
        self.book = Book(keep_ns) if self.lit else DarkBook()
        self.make_fee = int(cfg.make_fee)
        self.take_fee = int(cfg.take_fee)
        self.lat_ns = int(round(cfg.latency_us * NS_PER_US))
        self.jit_ns = int(round(cfg.jitter_us * NS_PER_US))
        self.feed_ns = int(round(cfg.feed_latency_us * NS_PER_US))
        self.bump_ns = int(round(cfg.bump_us * NS_PER_US))
        self.protect_ns = int(round(cfg.protect_us * NS_PER_US))
        self.ref_ns = int(round(cfg.ref_latency_us * NS_PER_US))
        self.react_lo = int(round(cfg.lp_react_us[0] * NS_PER_US))
        self.react_hi = int(round(cfg.lp_react_us[1] * NS_PER_US))
        self.snipe_lo = int(round(cfg.sniper_us[0] * NS_PER_US))
        self.snipe_hi = int(round(cfg.sniper_us[1] * NS_PER_US))
        self.lp_edge = int(cfg.lp_edge)
        self.lp_rate = float(cfg.lp_rate)
        self.lp_life_ns = cfg.lp_life_s * NS_PER_S
        self.dark_life_ns = cfg.dark_life_s * NS_PER_S
        # liquidity providers' view of fair value, expressed as the touch they will quote
        self.lp_bid = None
        self.lp_ask = None
        self.lp_target = None
        self.lp_target_seq = 0
        self.lp_applied_seq = 0
        # trade prints: (t, price, qty, aggressor side)
        self.prints = deque()
        # counters for validation statistics
        self.volume = 0
        self.volume_by_owner = {}

    def valid_touch(self, fair: float):
        """Best bid / ask an LP on this venue is willing to quote given fair value."""
        b = math.floor((fair - self.lp_edge) / TICK) * TICK
        a = math.ceil((fair + self.lp_edge) / TICK) * TICK
        return b, a

    def info(self) -> dict:
        return {
            "idx": self.idx, "name": self.name, "lit": self.lit, "make_fee": self.make_fee,
            "take_fee": self.take_fee, "lat_ns": self.lat_ns, "jit_ns": self.jit_ns, "feed_ns": self.feed_ns,
            "bump_ns": self.bump_ns, "protect_ns": self.protect_ns,
        }
