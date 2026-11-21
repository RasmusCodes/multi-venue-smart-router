"""Named routing strategies.

Each strategy is a configuration of the shared executor, so every comparison
holds the schedule, slicing and clean-up logic fixed and changes only the
component under study.
"""

from __future__ import annotations

from .executor import Executor
from .explore import Explorer

# name: (label, description, executor keyword arguments)
STRATEGIES = {
    # --- taking liquidity: how to route a marketable slice -------------------------------------
    "primary": ("Primary only", "Every slice crosses the spread on the primary venue alone, walking its book.",
                dict(split="take", take="primary")),
    "nbbo": ("Naive NBBO sweep", "Takes displayed NBBO liquidity across venues by price only (fee-blind), "
             "all child orders sent at once. The textbook naive smart router.",
             dict(split="take", take="nbbo")),
    "fee": ("Fee-aware sweep", "As the naive sweep, but ranks liquidity by all-in price (price plus taker fee, "
            "minus taker rebates).", dict(split="take", take="fee")),
    "sync": ("Synchronised sweep", "Fee-aware, and staggers send times so every child order reaches its venue "
             "at the same moment, leaving fast liquidity providers no time to fade quotes elsewhere.",
             dict(split="take", take="sync")),
    "model": ("Model-based sweep", "Synchronised and fee-aware; sizes each venue by its fitted probability that "
              "displayed liquidity survives until arrival, and pings the dark pool at the midpoint first.",
              dict(split="take", take="model", dark_ping=True)),
    # --- resting first: where to post, then cross for the remainder ------------------------------
    "post_primary": ("Post on primary", "Rests each slice at the NBBO on the primary venue, re-pegs if the "
                     "market moves away, crosses for the remainder at the end of the wait.",
                     dict(split="post", post="primary", take="sync")),
    "post_rebate": ("Post for rebate", "As above, on the venue paying the highest maker rebate.",
                    dict(split="post", post="rebate", take="sync")),
    "post_queue": ("Queue-aware post", "Rests where the expected cost per share is lowest given the queue ahead, "
                   "fees, the venue's fitted fill outcomes and the cost of crossing later for what does not fill.",
                   dict(split="post", post="queue", take="sync")),
    "post_adaptive": ("Adaptive post (bandit)", "Thompson sampling over venues on realised costs, learning "
                      "from scratch within the order.", dict(split="post", post="adaptive", take="sync")),
    # --- optimised split ---------------------------------------------------------------------------
    "ck": ("Cont-Kukanov split", "Each slice split between a market order and limit orders on several venues "
           "by minimising expected cost over fitted joint fill scenarios (Cont and Kukanov, 2017).",
           dict(split="ck", take="sync")),
    "smart": ("Smart router", "Cont-Kukanov split with queue-aware re-posting, model-based synchronised taking "
              "and a dark-pool ping: every component together.",
              dict(split="ck", take="model", post="queue", dark_ping=True)),
}

# benchmark naive variants used as comparators in the headline experiment
NAIVE = ("nbbo", "post_primary")


def make_strategy(name: str, stats=None, trace=False, **overrides):
    if name.startswith("explore"):
        mode = "joint" if name.endswith("joint") else "single"
        return Explorer(name=name, mode=mode, **overrides)
    try:
        label, _desc, kw = STRATEGIES[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; choose from {sorted(STRATEGIES)}") from None
    kw = dict(kw)
    kw.update(overrides)
    return Executor(name, stats=stats, trace=trace, **kw)


def label(name: str) -> str:
    return STRATEGIES[name][0] if name in STRATEGIES else name
