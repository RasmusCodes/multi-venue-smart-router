# Validation

Two kinds of checks: does the code do what it says (tests), and does the simulated market look like a
fragmented US equity market in the ways that matter for routing (stylised facts)?

## Tests

`python -m pytest` runs 49 tests in about 20 seconds (CI: Linux and Windows, Python 3.10, 3.12 and 3.13):

- **Order book**: price and time priority, partial fills, cancels, queue position; a differential test against
  a naive reference book over 12,000 random operations (identical fills and books after every step); the
  delayed-snapshot reconstruction checked against recorded books.
- **Dark book**: FIFO, midpoint limits, skipped orders keep their priority.
- **Market**: bit-identical reruns; news identical across routers on one seed (common random numbers); order
  and report latency; a cancel never overtakes its order; the router's snapshot equals the true book one feed
  latency ago; fees and rebates charged per venue; the speed bump delays marketable orders; conservation of
  filled shares; books never crossed.
- **Routers**: every strategy completes its parent order without over-filling; the naive sweep is fee-blind and
  the fee-aware one is not; synchronised send times align arrivals; the model sweep over-asks only by its
  survival rate and never beyond the parent's head-room; the Cont-Kukanov descent matches brute force on 25
  random problems; the post-model formula, its queue effect and its drift re-centring.
- **Metrics and statistics**: shortfall with fees and signs on hand-made fills; paired tests; Holm adjustment.
- **Golden digests**: four runs fingerprinted bit for bit, so any platform difference or unintended behaviour
  change fails.
- **Browser demo**: the demo's traced run (which advances the clock in 500 ms steps to sample the books)
  produces the same fingerprint and shortfall as a plain run.

## Stylised facts of the background market

`python -m smartrouter validate --seeds 20` (background only, no router, 20 seeds × 360 s, sampled every
100 ms after the warm-up):

- One-tick spread 97.7% of the time; mean quoted spread 2.05 bps.
- Volatility about 1.7% a day (from one-minute mid returns, 390-minute day).
- 24 news jumps and 31 cross-venue quote fades per minute.
- Dark pool share of volume 4.2%.

| Venue | Maker fee | Taker fee | At NBBO bid | At NBBO ask | Depth when at NBBO | Volume share |
|---|---:|---:|---:|---:|---:|---:|
| ALPHA | −$0.0020 | $0.0030 | 93% | 93% | 1,804 | 31.3% |
| BRAVO | −$0.0030 | $0.0030 | 98% | 99% | 2,326 | 32.1% |
| CHARLIE | $0.0020 | −$0.0015 | 47% | 43% | 504 | 15.9% |
| DELTA | −$0.0010 | $0.0010 | 85% | 83% | 764 | 16.6% |

| Aggressor | Share of volume |
|---|---:|
| Uninformed takers | 77.6% |
| Latency arbitrage | 21.9% |
| Dark-pool arrivals | 0.5% |

How these compare with the empirical literature:

| Fact | Literature | Here |
|---|---|---|
| Liquid stocks trade at a one-tick spread most of the time | Yao and Ye (2018) | 98% |
| Rebates buy queue length; inverted venues are thin and often off the NBBO | Chao, Yao and Ye (2019) | BRAVO deepest (2,326), CHARLIE thinnest (504) and at the NBBO under half the time |
| Limit orders on high-rebate venues are adversely selected | Battalio, Corwin and Jennings (2016) | 10 s markout of passive fills −0.46 bps on BRAVO, +0.24 on CHARLIE (E2) |
| A trade on one venue is followed by cancellations on others | van Kervel (2015) | 31 fades a minute; a naive sweep loses 19% of displayed liquidity |
| Latency-arbitrage races are about a fifth of volume | Aquilina, Budish and O'Neill (2022) | 22% |

Full references are in [MODEL.md](MODEL.md). The comparison is qualitative: the venues, fees and agents are
stylised, and a different, equally defensible parameter set would give different magnitudes.
