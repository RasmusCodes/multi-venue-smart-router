# The simulated market

One stock trades on five venues. The simulator is a discrete-event engine with integer nanosecond time
(`src/smartrouter/market.py`); everything a router does takes time to arrive, and everything it sees is
late.

## Units

| Quantity | Unit |
|---|---|
| Price | integer "pu" of $0.0001; one tick ($0.01) = 100 pu; the stock starts near $50.00 |
| Fee | pu per share; positive = paid, negative = rebate |
| Size | shares (lots of 100) |
| Time | integer nanoseconds |

## Venues (baseline)

| Venue | Type | Maker fee | Taker fee | Router latency (one way) | Notes |
|---|---|---:|---:|---:|---|
| ALPHA | maker-taker, primary | −$0.0020 | $0.0030 | 150 µs | closest to the router |
| BRAVO | maker-taker | −$0.0030 | $0.0030 | 300 µs | the highest maker rebate |
| CHARLIE | inverted | $0.0020 | −$0.0015 | 220 µs | takers are paid, makers pay |
| DELTA | low fee, asymmetric speed bump | −$0.0010 | $0.0010 | 380 µs + 350 µs bump on marketable orders | withdraws stale router pegs 30 µs after news |
| ECHO | dark midpoint pool | $0.0010 | $0.0010 | 260 µs | prices off the NBBO as it was 300 µs ago |

Names are fictional. Fees sit inside the historical Reg NMS access-fee cap of $0.0030 (the SEC lowered the
cap to $0.0010 for stocks priced at $1 or more in 2024; scenario `fee_cap_10` runs that world). Latencies
include up to 10% jitter; market data reaches the router with the same delay as its orders take to arrive.

Every lit venue matches by **price-time priority**: better prices first, then first come, first served
(`src/smartrouter/book.py`, checked against a naive reference book in `tests/test_book.py`). Orders can be
day orders, immediate-or-cancel (IOC) and post-only. The dark pool matches midpoint orders FIFO at its
reference midpoint, and never when that NBBO is locked or crossed.

**Speed bump and protection (DELTA).** An order that would take liquidity on arrival waits 350 µs before it
is matched; resting orders and cancels are not delayed. The venue also cancels resting router orders flagged
as pegs within 30 µs of news that makes them stale, before delayed arbitrageurs can reach them. This is the
mechanism behind asymmetric speed bumps and "crumbling quote" protections.

## Fair value

A latent fair value F (pu, a real number) moves for two reasons:

- **News**: compound Poisson jumps at 0.4 per second, of ±25, 50, 100 or 200 pu with probabilities
  0.40/0.30/0.20/0.10 (about 1.7% daily volatility measured from one-minute mid returns).
- **Order flow**: every lit trade by an uninformed taker or by the router moves F by κ = 0.01 pu per share
  in the trade's direction (permanent, Kyle-style impact; 20,000 shares move it 4 bps). Arbitrageurs' trades
  do not move F (their information is already in it), nor do dark trades.

Neither the router nor anyone else sees F directly; the router sees books and its own fills.

## Background traders

**Liquidity providers (LPs).** On each lit venue, LP orders arrive at a Poisson rate per side (5, 5, 2 and 2 per
second on ALPHA, BRAVO, CHARLIE, DELTA), at 0-4 ticks behind the touch the LPs will quote (probabilities
0.45/0.25/0.15/0.10/0.05), sized 100-500 shares, and live an exponential time (mean 4-12 s depending on the
venue) unless something else happens. The touch an LP will quote on venue v is the best price leaving it
`edge_v` pu of edge against fair value: `floor((F - edge)/tick)` for bids, `ceil((F + edge)/tick)` for asks.
Edges encode the fee economics: a large maker rebate lets LPs accept a thin edge (BRAVO 1 pu, ALPHA 6 pu), a
maker fee needs a fat one (CHARLIE 45 pu), and DELTA's protection makes a thin edge (3 pu) safe. That is why
BRAVO carries the longest queues and CHARLIE is at the NBBO only about half the time.

When fair value moves the quotable touch, LPs react after a venue-specific delay of 10-60 µs: they cancel the
quotes that are now stale or too far away, re-post 60% of the stale quantity at the new touch, and seed a new
better touch with 200 shares.

**Quote fades.** When a taker executes 500 shares or more on one venue, fast LPs on every other venue see the
print after their reaction delay and pull half of their quantity (30% on CHARLIE) at the prices that taker
reached, re-posting it one tick further away. This is the "phantom liquidity" a router meets when its child
orders arrive at different venues at different times.

**Uninformed takers.** Marketable orders arrive at 2 per second, buy or sell with equal odds, sized 100-2,000
shares (mean 400). Half rank venues by all-in price (fee-aware), half pick a venue in proportion to its
displayed size; 20% ping the dark pool first; they sweep up to two ticks through the NBBO. They are
co-located: their orders reach venues instantly, except through DELTA's bump.

**Latency arbitrageurs.** On every news jump they send IOC orders to each lit venue for all quotes that the
jump made stale by more than the taker fee plus 5 pu, arriving after 20-60 µs. Whether they or the LPs'
cancels arrive first is a race decided by the two delays. On news of 100 pu or more they also pick off resting
dark orders at the pool's stale midpoint. They account for about 22% of volume, in line with the roughly
one-fifth share of volume in latency-arbitrage races that Aquilina, Budish and O'Neill (2022) measure on the
London Stock Exchange.

**Dark pool.** Resting midpoint orders of 200-2,000 shares arrive at 0.04 per second per side and live 60 s on
average.

## The router's view

A strategy only uses `RouterContext` (`src/smartrouter/router/context.py`):

- `snapshot(v)`: the top five levels of venue v as they were one feed latency ago, rebuilt exactly from a
  mutation log (tested against recorded books);
- `send` / `cancel`: messages that take the venue's latency plus jitter to arrive, FIFO per venue (a cancel
  never overtakes its order);
- execution reports (ack, fill, done, canceled, reject), which take the same time to come back;
- timers.

## Determinism and common random numbers

Every random draw comes from a named stream (`src/smartrouter/detmath.py`): news, uninformed takers, each
venue's LPs, the dark pool, network jitter. Each event consumes a fixed number of draws, and delays tied to
identifiable events (an LP reaction to the k-th news jump, an arbitrageur's race on venue v) are keyed hashes,
so two routers run on the same seed see the same news, the same background order arrivals and the same LP
decisions until their own orders change the state. That makes paired comparisons very precise: in E1 the
standard deviation of a router's shortfall across seeds is about 9.5 bps, but that of its difference from
another router is 0.2 to 0.3 bps.

The simulation path uses only IEEE-754 basic arithmetic (logarithms and exponentials are computed from
`+ - * /`), so runs are bit-identical across platforms; `tests/test_golden.py` locks fingerprints of four runs.
The browser demo runs the same package in Pyodide and reproduces them.

## Parameters and their sources

The parameters are stylised, chosen so that the background market shows the facts below (see
[VALIDATION.md](VALIDATION.md)); they are not fitted to a particular stock.

| Fact | Literature | Simulated |
|---|---|---|
| Large-cap spreads pinned at one tick | Yao and Ye (2018); Chao, Yao and Ye (2019) | one tick 98% of the time |
| Rebates buy queue length, inverted venues stay thin | Chao, Yao and Ye (2019) | BRAVO 2,300 shares at the NBBO, CHARLIE 500 |
| Limit orders on high-rebate venues are adversely selected | Battalio, Corwin and Jennings (2016) | 10 s markout of passive fills: BRAVO −0.46 bps, CHARLIE +0.24 |
| A trade on one venue triggers cancellations on others | van Kervel (2015) | 31 fades a minute |
| Latency arbitrage is about a fifth of volume | Aquilina, Budish and O'Neill (2022) | 22% |

## References

- Aquilina, M., Budish, E., O'Neill, P. (2022). Quantifying the high-frequency trading "arms race". *Quarterly
  Journal of Economics*.
- Battalio, R., Corwin, S., Jennings, R. (2016). Can brokers have it all? On the relation between make-take
  fees and limit order execution quality. *Journal of Finance*.
- Budish, E., Cramton, P., Shim, J. (2015). The high-frequency trading arms race: frequent batch auctions as a
  market design response. *Quarterly Journal of Economics*.
- Chao, Y., Yao, C., Ye, M. (2019). Why discrete price fragments U.S. stock exchanges and disperses their fee
  structures. *Review of Financial Studies*.
- Cont, R., Kukanov, A. (2017). Optimal order placement in limit order markets. *Quantitative Finance*.
- Kyle, A. (1985). Continuous auctions and insider trading. *Econometrica*.
- van Kervel, V. (2015). Competition for order flow with fast and slow traders. *Review of Financial Studies*.
- Yao, C., Ye, M. (2018). Why trading speed matters: a tale of queue rationing under price controls. *Review of
  Financial Studies*.
