# W2 — Robustness battery (design)

Status: design, 2026-08-17. Follows W1 (execution-engine unification, `115b652`).
Target module: `evaluation/robustness.py`.

## 1. Purpose

`evaluation/stats.py` already carries a three-tier significance battery: parametric
(IC, quantile spread), resampling (block bootstrap, trade-level permutation, BH-FDR),
and research-grade (walk-forward, regime conditioning, deflated Sharpe). What it does
not carry is the class of tests that ask **"is this result an artifact of the exact
price path, the exact trade sequence, or the number of configurations I looked at?"**

Five methods close that gap. Each answers a question none of the existing tests answer:

| Method | Question it answers | Existing test that does NOT answer it |
|---|---|---|
| Noise test | Does the edge survive small perturbations of the price level? | Nothing. Every current test runs on one exact price path. |
| Price-series MCPT | Does the *signal* carry information, or would any rule score this well on a series with these return moments? | `permutation_trades` holds prices fixed and moves signals — the mirror image. |
| Trade-order MC | Is the observed drawdown a property of the strategy or of the order the trades happened to arrive in? | Nothing. `bootstrap_sharpe` resamples daily returns, not trade sequence. |
| PBO (CSCV) | Given how many configurations were compared, how often does the in-sample winner underperform out-of-sample? | `deflated_sharpe` adjusts a *single* Sharpe for trial count; PBO measures selection skill directly. |
| CPCV | Do walk-forward results hold across many train/test partitions, with leakage purged? | `walk_forward` is one expanding-window path with no purge and no embargo. |

## 2. Design constraints

- **House rule carries over.** A statistic whose assumptions fail returns `None` plus a
  `*_reason` string. Never divide by a zero/NaN sd.
- **Every re-simulation goes through the W1 engine** with the caller's `ExecutionConfig`.
  A null that pays no costs while the strategy pays 10 bps is not a null. W1 Step B
  already fixed this for `permutation_trades`; the same rule binds every method here.
- **Deterministic under a seed.** All randomness through `np.random.default_rng(seed)`.
- **No new dependencies.** numpy / pandas / scipy only.

## 3. Method specifications

### 3.1 `noise_test(rule, cache, *, n_trials, sigma_bps, seed, config)`

Multiply every bar's OHLC by a per-bar lognormal shock, `exp(N(0, sigma))`, and re-run
the rule end-to-end so signals are recomputed on the perturbed series.

**The shock is applied as one common factor per bar, to open/high/low/close together.**
That is deliberate: it jitters the *price level* of each bar while leaving intrabar
geometry (`high >= max(open, close)`, etc.) exactly intact. Perturbing the four
independently would break those invariants and any rule reading `high`/`low` would be
reacting to impossible bars rather than to noise. Volume is untouched.

Reports the distribution of net P&L across trials and `pct_profitable` — the fraction of
trials still net-positive. A strategy whose edge evaporates under 5 bps of price jitter is
fitted to tick-level accidents.

### 3.2 `price_mcpt(rule, cache, *, n_perm, seed, config)`

Shuffle each symbol's close-to-close log returns, rebuild a synthetic path from the same
first price, re-run the rule on the synthetic series, and compare total net P&L.
One-sided empirical p with the `+1` correction, as elsewhere in this repo.

OHLC is reconstructed by preserving each bar's own ratio to its close (`open/close`,
`high/close`, `low/close` travel with the bar as it is reshuffled), so intrabar geometry
survives the permutation.

**Stated limitation, because this statistic is easy to over-read:** shuffling returns
destroys serial dependence as well as the signal's information. A trend-following rule
will therefore score well against this null partly *because* the null has no trends, not
only because the rule has an edge. Price MCPT is evidence that the rule is not scoring on
return moments alone; it is not by itself evidence of forecasting skill. Read it beside
`permutation_trades`, which nulls the signal while leaving the price path — including its
autocorrelation — untouched. The two together are informative; either alone is not.

### 3.3 `trade_order_mc(trades, *, n_trials, seed, starting_equity)`

Take realized `pnl_pct` values, shuffle their order, compound an equity curve per trial,
and report the distribution of max drawdown and final return.

**Corrected during implementation — this section's original draft was wrong.** It claimed
compounding would make *both* drawdown and final return vary, where summing dollars would
vary only drawdown. False: final equity is `prod(1 + r_i)` and multiplication commutes, so
final return is identical under every permutation, exactly as a dollar sum is. A unit test
asserting spread on the final-return percentiles caught it (`assert 7.08 > 7.08`).

Consequence for the API: `final_return_pct` is reported **once**, with no percentile band.
Publishing p5/p95 on an order-invariant quantity would be a zero-width band dressed up as
uncertainty. Drawdown — path-dependent statistics generally — is the only thing trade-order
permutation can move, and is the whole point of the test. Compounding is still the right
choice, but for a different reason than the one originally given: it matches how the
capital is actually at risk along the path.

Reports observed max drawdown alongside its percentile in the shuffled distribution. An
observed drawdown at the 5th percentile means the live sequence was *kind*; plan for the
median, not for what happened.

### 3.4 `pbo(returns_matrix, *, n_splits, metric)`

Bailey/Borwein/López de Prado combinatorially-symmetric cross-validation. Input is a
`(T, N)` matrix: rows are periods, columns are the N configurations that were compared.

Split rows into `n_splits` contiguous groups; for each of the `C(S, S/2)` ways to choose
half the groups as in-sample, the complement is out-of-sample. Find the IS-best column,
take its OOS rank `r` among N, map to `logit(r / (N+1))`. **PBO is the fraction of
combinations whose logit is below zero** — i.e. how often the in-sample winner lands in
the bottom half out-of-sample. PBO near 0.5 means selection is a coin flip.

Requires `N >= 2` columns and `n_splits` even and `>= 4` (`C(4,2) = 6` combinations is the
practical floor for a meaningful fraction).

**Measured during implementation, and not what the textbook framing suggests:** "no skill
implies PBO ≈ 0.5" is an expectation *over datasets*, not a property of any one dataset.
Across eight pure-noise realizations (T=600, N=20, S=8) PBO ranged **0.24 to 0.81**. Two
things drive that: the winner's out-of-sample rank is genuinely noisy at N=20, and because
IS and OOS are complementary halves of one fixed dataset, each column's halves are
mechanically anticorrelated around its full-sample mean (pooled IS/OOS correlation measured
at −0.23 on noise). A single PBO of 0.62 is therefore not meaningfully worse than 0.48, and
the docstring says so. What PBO *does* separate reliably is genuine dominance: a column
with a real edge drives PBO below 0.05 on every realization tested.

### 3.5 `cpcv_splits(n_obs, *, n_groups, k_test, embargo_pct, t1)`

Combinatorial purged cross-validation split generator. Yields `(train_idx, test_idx)`
pairs for every way of choosing `k_test` of `n_groups` contiguous groups as test.

Two leakage controls, both required for this to mean anything:

- **Purge**: drop any training observation whose label window overlaps the test window.
  With `t1` supplied (per-observation label end index) the overlap is exact; without it,
  purging falls back to the embargo alone and the return value says so.
- **Embargo**: additionally drop `embargo_pct` of `n_obs` training observations
  immediately *after* each test block, covering serial correlation that outlives the
  label itself.

Returns a generator plus a companion `cpcv_report` describing the purge that was applied,
so a caller cannot silently get un-purged splits and believe otherwise.

## 4. Out of scope

- Wiring into `evaluate.py` / the runner grid. This lands as a library plus tests; the
  campaign integration is a separate decision — every one of these is a *secondary*
  diagnostic and the pre-registration protocol pins one primary test per strategy.
  Adding these to the primary endpoint would be protocol drift, not extra rigor.
- The reporting layer (W3) renders none of this yet.

## 5. Exit criteria

- `evaluation/robustness.py` with the five methods, each returning the house-rule dict.
- Unit tests covering: determinism under seed, the OHLC invariant under noise, the
  documented "sum is invariant / compounding is not" property, PBO ≈ 0.5 on pure noise
  columns and PBO ≈ 0 on a genuinely-dominant column, CPCV purge actually removing
  overlapping training indices, and every `*_reason` early-return path.
- Full suite green with zero edits to existing assertions.
