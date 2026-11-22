# The routers

All strategies share one executor (`src/smartrouter/router/executor.py`), so a comparison changes only the
component under study. Named configurations live in `src/smartrouter/router/registry.py`.

## Shared schedule

The parent order (20,000 shares, buy or sell, over 300 seconds) is cut into equal slices: 20 slices of 1,000
(patient) or 5 of 4,000 (urgent). At each slice the executor tops the order up to the schedule's cumulative
target with one of three tactics:

| Tactic | What happens |
|---|---|
| `take` | Cross the spread for the slice at once (a *take policy* routes it). |
| `post` | Rest the slice at the NBBO on one venue (a *post policy* picks it) for 11.25 s, re-peg every 500 ms if the market moves away, then cross the spread for whatever the schedule still needs. |
| `ck` | Split the slice between a market order and limit orders on several venues (Cont-Kukanov), then the same clean-up. |

Whatever is unfilled at the end of the horizon is taken with a limit that widens one tick per empty sweep. The
executor never has more shares filled or working than the parent quantity, and never works more than one
slice ahead of the schedule.

## Taking: routing a marketable slice

Each sweep reads the delayed books of the lit venues, lists every displayed level within one tick of the
NBBO, ranks them, allocates the quantity greedily and sends one IOC per venue limited at the deepest price
allocated there. Unfilled remainders come back and are swept again with fresh data.

| Policy | Ranking | Send times |
|---|---|---|
| `primary` | the primary venue's book only | at once |
| `nbbo` | displayed price, ties to the primary venue | all at once |
| `fee` | all-in price: price plus taker fee (minus a taker rebate), then latency | all at once |
| `sync` | as `fee` | staggered: venue v is sent `max latency − latency_v` later (speed bump included), so every child order arrives at the same moment |
| `model` | as `sync` | as `sync`; each venue is asked for enough that requested × survival covers what is needed, where survival is the fitted share of a synchronised IOC that fills there; and the dark pool is pinged at the midpoint first |

## Resting: choosing a venue

The post price is the NBBO on the order's own side. Allowed venues are those where a post-only order would
not cross the displayed book.

| Policy | Venue |
|---|---|
| `primary` | the primary venue |
| `rebate` | the highest maker rebate |
| `queue` | the lowest expected cost per share (below) |
| `adaptive` | Thompson sampling: a Gaussian posterior of realised cost per share per venue, starting uninformed |

**Expected cost of a post.** On training seeds an explorer posts orders at random venues and records, for each:
the volume that traded at its price level on that venue during the wait (`exec`), the share of the queue ahead
that cancelled (`cf`), and, from the router's own delayed view at the end of the wait, `dm` = side × (post price
− mid) and `hsT` = the half spread then. For a new order of q shares behind Q:

```
filled = min(q, max(0, exec − (1 − cf) · Q))
cost   = filled · (dm + maker fee) + (q − filled) · (hsT + taker fee)      per sample; averaged; divided by q
```

Costs are measured against the mid at the end of the wait: the parent's shortfall equals the sum of these
end-of-wait costs plus a drift term no routing choice affects. A fill just before the price runs through the
order has a large `dm` (adverse selection); a non-fill pays the spread crossing. The market's drift is
re-centred to mean zero per venue (fair value is a martingale), so that sampling noise in the drift does not
masquerade as a venue difference while the link between filling and the drift is kept.

## Splitting: Cont and Kukanov

Following Cont and Kukanov (2017), choose a market order M and limit orders L_k on venues k behind queues Q_k to
minimise the expected cost

```
V = take_cost(M) + Σ_k c_k · fill_k + λ_u · (S − A)⁺ + λ_o · (A − S)⁺
fill_k = min(L_k, (ξ_k − Q_k)⁺),   A = M + Σ_k fill_k
```

where ξ_k is the queue outflow at venue k over the wait. The expectation is a sample average over 150 joint
scenarios recorded on training seeds by posting on every lit venue at once, with prices against the end-of-wait
mid scenario by scenario: c_k = side × (limit price − end mid) + maker fee_k, λ_u = end half spread + taker fee,
and λ_o = minus the expected cost of executing a share later (an over-filled share replaces a later one). The
take cost walks the consolidated delayed book by all-in price. The objective is convex in (M, L); steepest
descent over 100-share moves (including shifting a lot from the market order to a venue) finds the optimum,
checked against brute force in `tests/test_router.py`.

## The smart router

`smart` = Cont-Kukanov split per slice + queue-aware re-posting + the `model` sweep for the market-order part
and the clean-up + a dark-pool ping. It is the "everything we know" router; the experiments ask which parts of
it earn their keep.

## What routers learn from

Only training seeds (900,000 and up): 200 seeds of single-venue exploration posts (about 1,250 samples per
venue), 200 seeds of joint posts (about 5,000 scenarios) and 100 seeds of synchronised sweeps (for survival
rates and the average clean-up fee). Each market regime is calibrated separately. Evaluation seeds (1 to 300)
are never used for fitting. The explorer measures queue outflow by reading the venue's book directly, which a
live router could not do; in practice the same quantities would come from the router's own fill history.

## Fitted statistics, baseline market

| Venue | Survival of a synchronised IOC | Mean queue ahead of a post | Volume traded at its level in 11.25 s | Share of the queue ahead that cancelled |
|---|---:|---:|---:|---:|
| ALPHA | 98.9% | 1,637 | 1,135 | 57% |
| BRAVO | 97.5% | 2,271 | 1,193 | 60% |
| CHARLIE | 97.5% | 219 | 826 | 24% |
| DELTA | 96.4% | 643 | 630 | 52% |

Average taker fee paid in clean-up sweeps: $0.0019 per share.
