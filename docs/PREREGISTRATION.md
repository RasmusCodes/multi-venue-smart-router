# Pre-registration

Written on 2026-11-21, before any evaluation seed was run. The commit that adds this file freezes the
simulator, the routers, the calibration procedure and the experiment definitions
(`src/smartrouter/experiments.py`). Results are reported against this document; any change made after
seeing evaluation results is listed under *Deviations* in [the results report](../reports/RESULTS.md), with
the pre-registered result shown alongside.

## Question

Can intelligent routing materially improve execution quality after accounting for fees, rebates, queue
position, latency, fill probability, adverse selection and market impact?

## What happened before this document

- The market model and the routers were built and debugged on **development seeds** (500,000 and up), 40
  seeds per experiment. Development runs found and fixed three router flaws: the model-based sweep
  requested a whole price level instead of what it needed; clean-up after a Cont-Kukanov slice crossed the
  spread for deliberate over-posting; and the over-fill penalty treated extra passive fills as free profit.
  They also led to measuring passive-order costs against the mid at the end of the wait, with the market
  drift re-centred, and to giving the calibration probes the same venue protection as real posts.
- Development results suggested the effect sizes behind the hypotheses below. Evaluation uses fresh seeds.
- Model-based routers learn only from **training seeds** (900,000 and up), never from evaluation seeds.

## Design

- **Evaluation seeds** 1-300 (E1-E4) and 1-200 (E5, E6). Every strategy runs on the same seeds; comparisons
  are paired by seed (common random numbers).
- **Primary metric**: implementation shortfall (IS) of the whole parent order, in basis points of the arrival
  mid, fees and rebates included. Lower is better. For each comparison, the per-seed difference
  strategy minus comparator is tested with a two-sided paired t-test; a 10,000-draw bootstrap interval and a
  Wilcoxon signed-rank test are reported as checks.
- **Multiple testing**: Holm-Bonferroni across the nine primary hypotheses (family-wise alpha 0.05).
- **Materiality**: an improvement is *material* if the mean saving is at least **0.25 bps** and the
  Holm-adjusted p-value is below 0.05. A significant saving below 0.25 bps is reported as *detectable but
  immaterial*. 0.25 bps is $25 per $1 million traded, about $6 million a year for a desk trading $1 billion
  a day.
- **Stopping rule**: each experiment runs once with the seeds above. No seeds are added or dropped after
  seeing results.

## Primary hypotheses (direction stated, Holm-corrected together)

| | Experiment | Hypothesis |
|---|---|---|
| P1 | E1, taking | The synchronised fee-aware sweep (`sync`) has lower IS than the naive NBBO sweep (`nbbo`). |
| P2 | E1, taking | The fee-aware sweep sent all at once (`fee`) has lower IS than `nbbo`. |
| P3 | E1, taking | The model-based sweep (`model`: fill-probability sizing plus dark ping) has lower IS than `sync`. |
| P4 | E2, posting | Queue-aware posting (`post_queue`) has lower IS than posting on the primary venue (`post_primary`). |
| P5 | E3, splitting | The Cont-Kukanov split (`ck`) has lower IS than `post_queue`. |
| P6 | E4, patient | The full smart router (`smart`) has lower IS than `post_primary`. |
| P7 | E4, urgent | `smart` has lower IS than `post_primary`. |
| P8 | E4, patient | `smart` has lower IS than `nbbo`. |
| P9 | E4, urgent | `smart` has lower IS than `nbbo`. |

## Secondary hypotheses (reported with their own p-values, Holm within each group)

- **S1, the rebate trap.** Passive fills of `post_rebate` (highest maker rebate) have worse 10-second markouts
  than those of `post_primary` and of `post_queue`, and `post_rebate` does not have lower IS than
  `post_primary` (its 95% interval includes zero or lies above it).
- **S2, robustness.** In each E5 regime (access fees capped at $0.0010, faster fading, higher volatility,
  wider latency gaps) `smart` has lower IS than `post_primary` and than `nbbo`.
- **S3, negative control.** With four identical venues and no dark pool (E6), `sync` and `nbbo` behave
  identically, and `smart` is within ±0.25 bps of `post_primary` (two one-sided tests, alpha 0.05 each).
- **S4, learning.** Thompson-sampling posting (`post_adaptive`) has lower IS than `post_primary` and higher IS
  than `post_queue`.

## What would count against intelligent routing

Primary hypotheses failing, savings below the materiality threshold, the negative control showing gains
(which would point to an artefact rather than routing skill), or gains that appear only in the baseline
regime.

## Known limitations, stated in advance

- The market is simulated. Its parameters are stylised and checked against qualitative facts from the
  empirical literature (one-tick spreads, longer queues on high-rebate venues, latency-arbitrage share of
  volume, quote fading), not fitted to a specific stock.
- Passive orders have no permanent price impact in this model (only taker volume moves fair value), so the
  gap between passive and aggressive *tactics* is larger than it would be in a market where resting demand
  leaks. Venue comparisons at a fixed tactic are less exposed to this.
- Liquidity providers follow fixed rules; they do not learn about the router.
