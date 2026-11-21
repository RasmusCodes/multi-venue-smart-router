"""Scenario configuration: venues, background market and the parent order.

Units used everywhere in the engine:

* price: integer "price units" (pu) of $0.0001; one tick ($0.01) is 100 pu
* fee: pu per share, positive = the participant pays, negative = rebate
* quantity: shares
* time: integer nanoseconds (configs are written in microseconds / seconds)
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field, fields

TICK = 100  # pu per tick ($0.01)
NS_PER_US = 1_000
NS_PER_S = 1_000_000_000


@dataclass
class VenueConfig:
    name: str
    kind: str = "lit"  # "lit" (displayed limit order book) or "dark" (midpoint pool)
    description: str = ""
    make_fee: int = -20  # pu/share; negative = rebate to the liquidity provider
    take_fee: int = 30  # pu/share; negative = rebate to the taker (inverted venue)
    latency_us: float = 200.0  # router <-> venue, one way
    jitter_us: float = 20.0  # extra one-way delay ~ U[0, jitter]
    feed_latency_us: float = 200.0  # venue -> router market data
    bump_us: float = 0.0  # asymmetric speed bump on marketable inbound orders
    protect_us: float = 0.0  # >0: venue withdraws stale router pegs this fast after news
    # background liquidity providers (lit venues)
    lp_rate: float = 4.0  # new LP orders per second per side
    lp_edge: int = 5  # pu of edge vs fair value an LP needs to quote (lower when rebates are high)
    lp_size_mult: float = 1.0
    lp_life_s: float = 8.0  # mean resting time before a random cancel
    lp_react_us: tuple = (10.0, 60.0)  # reaction to fair-value changes ~ U[lo, hi]
    lp_fade: float = 0.5  # share of touch quantity LPs pull when a large trade prints on another venue
    sniper_us: tuple = (20.0, 60.0)  # latency arbitrageurs' order transit ~ U[lo, hi]
    # dark pool (kind == "dark")
    dark_rate: float = 0.0  # resting midpoint orders per second per side
    dark_life_s: float = 60.0
    dark_size_mult: float = 1.0
    ref_latency_us: float = 300.0  # age of the NBBO the pool prices its midpoint from

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lp_react_us"] = list(self.lp_react_us)
        d["sniper_us"] = list(self.sniper_us)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "VenueConfig":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        for key in ("lp_react_us", "sniper_us"):
            if key in kw:
                kw[key] = tuple(kw[key])
        return cls(**kw)


@dataclass
class MarketConfig:
    f0: int = 500_037  # initial fair value, pu ($50.0037)
    jump_rate: float = 0.4  # news jumps per second
    jump_sizes: list = field(default_factory=lambda: [25, 50, 100, 200])
    jump_probs: list = field(default_factory=lambda: [0.40, 0.30, 0.20, 0.10])
    kappa: float = 0.01  # permanent impact, pu of fair value per share of non-arbitrage taker volume
    noise_rate: float = 2.0  # uninformed marketable orders per second
    noise_sizes: list = field(default_factory=lambda: [100, 200, 300, 500, 1000, 2000])
    noise_probs: list = field(default_factory=lambda: [0.30, 0.25, 0.15, 0.15, 0.10, 0.05])
    noise_fee_aware: float = 0.5  # share of noise takers that rank venues by all-in price
    noise_dark_prob: float = 0.2  # share that pings the dark pool first
    noise_max_ticks: int = 2  # how far through the NBBO a noise taker will sweep
    lp_levels: list = field(default_factory=lambda: [0.45, 0.25, 0.15, 0.10, 0.05])
    lp_sizes: list = field(default_factory=lambda: [100, 200, 300, 500])
    lp_size_probs: list = field(default_factory=lambda: [0.30, 0.30, 0.25, 0.15])
    lp_repost: float = 0.6  # share of quantity cancelled as stale that LPs re-post at the new touch
    lp_improve_qty: int = 200  # posted at once when a new, better touch becomes valid
    lp_max_levels: int = 8  # LP orders further than this from the touch are withdrawn
    fade_min_qty: int = 500  # a taker execution this large on one venue makes LPs elsewhere fade
    sniper_edge: int = 5  # pu an arbitrageur needs after fees
    sniper_max_qty: int = 5000
    dark_snipe_min_jump: int = 100  # pu; smaller news is not worth racing a midpoint for
    dark_sizes: list = field(default_factory=lambda: [200, 500, 1000, 2000])
    dark_size_probs: list = field(default_factory=lambda: [0.35, 0.35, 0.20, 0.10])
    warmup_s: float = 60.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ParentConfig:
    side: int = 0  # +1 buy, -1 sell, 0 = drawn per seed
    qty: int = 20_000
    horizon_s: float = 300.0
    slices: int = 20
    tail_s: float = 60.0  # simulated after the horizon so late fills have markouts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ParentConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Scenario:
    name: str
    venues: list
    market: MarketConfig = field(default_factory=MarketConfig)
    parent: ParentConfig = field(default_factory=ParentConfig)
    description: str = ""

    def copy(self) -> "Scenario":
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "venues": [v.to_dict() for v in self.venues],
            "market": self.market.to_dict(),
            "parent": self.parent.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            venues=[VenueConfig.from_dict(v) for v in d["venues"]],
            market=MarketConfig.from_dict(d.get("market", {})),
            parent=ParentConfig.from_dict(d.get("parent", {})),
        )

    def venue_index(self, name: str) -> int:
        for i, v in enumerate(self.venues):
            if v.name == name:
                return i
        raise KeyError(name)


# ----------------------------------------------------------------------------- presets


def baseline_venues() -> list:
    """Five stylised venues spanning the fee and latency structures of US equities.

    Names are fictional. Fees sit inside the historical Reg NMS access-fee cap of
    $0.0030 (30 pu). LP edges encode the economics: a maker rebate lets liquidity
    providers accept a thinner edge against fair value, a maker fee needs a fatter one.
    """
    return [
        VenueConfig(
            name="ALPHA", description="Primary maker-taker exchange, closest to the router",
            make_fee=-20, take_fee=30, latency_us=150, jitter_us=15, feed_latency_us=150,
            lp_rate=5.0, lp_edge=6, lp_life_s=8.0,
        ),
        VenueConfig(
            name="BRAVO", description="High-rebate maker-taker exchange: long queues, farther away",
            make_fee=-30, take_fee=30, latency_us=300, jitter_us=30, feed_latency_us=300,
            lp_rate=5.0, lp_edge=1, lp_life_s=12.0,
        ),
        VenueConfig(
            name="CHARLIE", description="Inverted exchange: takers earn a rebate, makers pay",
            make_fee=20, take_fee=-15, latency_us=220, jitter_us=20, feed_latency_us=220,
            lp_rate=2.0, lp_edge=45, lp_life_s=4.0, lp_fade=0.3,
        ),
        VenueConfig(
            name="DELTA", description="Low-fee exchange with an asymmetric 350 us speed bump on takers",
            make_fee=-10, take_fee=10, latency_us=380, jitter_us=40, feed_latency_us=380,
            bump_us=350, protect_us=30, lp_rate=2.0, lp_edge=3, lp_life_s=8.0,
        ),
        VenueConfig(
            name="ECHO", kind="dark", description="Dark midpoint pool priced off a 300 us old NBBO",
            make_fee=10, take_fee=10, latency_us=260, jitter_us=25, feed_latency_us=260,
            dark_rate=0.04, dark_life_s=60.0, ref_latency_us=300,
        ),
    ]


def baseline() -> Scenario:
    return Scenario(
        name="baseline",
        description="Four lit venues with different fees, latencies and queue lengths plus a dark midpoint pool.",
        venues=baseline_venues(),
    )


def homogeneous() -> Scenario:
    """Negative control: four identical lit venues (same fees, latency, liquidity), no dark pool."""
    venues = []
    for name in ("ALPHA", "BRAVO", "CHARLIE", "DELTA"):
        venues.append(VenueConfig(
            name=name, description="Identical maker-taker venue",
            make_fee=-20, take_fee=30, latency_us=250, jitter_us=25, feed_latency_us=250,
            lp_rate=3.25, lp_edge=6, lp_life_s=8.0,
        ))
    return Scenario(name="homogeneous", description="Negative control: identical venues.", venues=venues)


def fee_cap_10() -> Scenario:
    """Access fees capped at 10 pu ($0.0010), rebates scaled to keep each venue's net capture."""
    sc = baseline()
    sc.name = "fee_cap_10"
    sc.description = "Access fees capped at $0.0010; rebates and the inverted venue's pricing scaled down."
    for v in sc.venues:
        if v.kind != "lit":
            continue
        if v.name == "ALPHA":
            v.make_fee, v.take_fee, v.lp_edge = -6, 10, 18
        elif v.name == "BRAVO":
            v.make_fee, v.take_fee, v.lp_edge = -9, 10, 15
        elif v.name == "CHARLIE":
            v.make_fee, v.take_fee, v.lp_edge = 6, -5, 30
        elif v.name == "DELTA":
            v.make_fee, v.take_fee, v.lp_edge = -3, 5, 8
    return sc


def fast_fade() -> Scenario:
    """Faster liquidity providers and stronger impact: displayed liquidity fades sooner."""
    sc = baseline()
    sc.name = "fast_fade"
    sc.description = "LPs react twice as fast and order flow moves fair value twice as much."
    sc.market.kappa = 0.02
    for v in sc.venues:
        lo, hi = v.lp_react_us
        v.lp_react_us = (lo / 2, hi / 2)
    return sc


def high_vol() -> Scenario:
    sc = baseline()
    sc.name = "high_vol"
    sc.description = "News jumps twice as often (volatility up about 41%)."
    sc.market.jump_rate = 0.8
    return sc


def wide_latency() -> Scenario:
    """Router far from two of the venues: latency differences triple."""
    sc = baseline()
    sc.name = "wide_latency"
    sc.description = "Latency gaps between venues tripled (router co-located with ALPHA only)."
    for v in sc.venues:
        if v.name != "ALPHA":
            v.latency_us = 150 + 3 * (v.latency_us - 150)
            v.feed_latency_us = v.latency_us
    return sc


PRESETS = {
    "baseline": baseline,
    "homogeneous": homogeneous,
    "fee_cap_10": fee_cap_10,
    "fast_fade": fast_fade,
    "high_vol": high_vol,
    "wide_latency": wide_latency,
}


def preset(name: str) -> Scenario:
    try:
        return PRESETS[name]()
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; choose from {sorted(PRESETS)}") from None
