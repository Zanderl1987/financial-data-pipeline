# Per-symbol ADV market-impact cost for backtest.py's weight-matrix engine

**Date:** 2026-09-03
**Feature:** `backtest.py`'s new `adv_participation_coeff` / `_adv_participation_cost()`
**Data:** 10-symbol basket (AAPL, MSFT, SPY, XOM, JPM, KO, PG, AMZN, NVDA, GOOGL), real
`prices` table, 2020-2025

## Question

TASKS.md's ADV market-impact follow-up: "Consider applying the same participation-based
model to `backtest.py`'s weight-matrix engine (currently portfolio-turnover based with no
per-symbol liquidity concept) — deliberately not done this session, but the machinery
(`load_dollar_volume_matrix`) now exists and could be reused." Built earlier this session
for `event_backtest.scenario()`, never wired into the weight-matrix engine.

## Method

Added `adv_participation_coeff` (opt-in, default `None`) to `backtest()`: for each
rebalance day, converts each symbol's fractional weight CHANGE into a dollar amount using
a new `aum` parameter (this engine has no other dollar-size concept — it's pure
weights/returns), compares that against the symbol's own trailing `adv_window`-day
average dollar volume, and charges `coeff/1e4 * sqrt(participation)` on that dollar
amount — summed across symbols, added to the existing (portfolio-turnover-based, no
per-symbol concept) cost series. This is the SAME model `event_backtest.scenario()`'s
`adv_impact_coeff` already uses, reused rather than reinvented, but kept as a distinctly
NAMED parameter (`adv_participation_coeff`, not `adv_impact_coeff`) since backtest.py
already has an `adv_impact_coeff` meaning something else (portfolio turnover, no
per-symbol liquidity) — repeating that exact ambiguity a third way was the one thing to
avoid, per `evaluation/execution.py`'s own docstring on why the two existing meanings of
"sqrt_impact" are kept apart.

Verified: (1) the full suite with zero unrelated assertion edits, (2) synthetic tests for
the wiring (off by default, missing volume data degrades to zero extra cost — same
established precedent as `event_backtest.scenario()`'s own missing-volume handling, not a
new behavior — higher coefficient costs more), (3) two real-data runs at different scales.

## Results

**Realistic scale** (10 liquid large-caps, monthly rebalance, $5M AUM, coeff=5.0): CAGR
moved from -5.62% to -5.66% — a small, correctly-signed (negative) effect. Makes economic
sense: monthly turnover on $5M against AAPL/MSFT/SPY-class daily dollar volume is a tiny
participation rate, so the impact cost is genuinely small. This is the useful case this
feature actually targets — most of this repo's factor-signal backtests run at modest
notional against liquid names, where the existing portfolio-turnover cost model already
captures most of the real cost and per-symbol ADV should be a minor correction, not a
dominant one.

**Stress-test scale** (same universe, daily rebalance, $500M AUM, coeff=50.0): CAGR
flipped from +19.63% to -16.81% — a 73.6 percentage-point total-return gap. Confirms the
mechanism has real bite when participation is actually high (a large book trading
frequently against real, finite liquidity), not just a token nonzero number.

## Verdict

**Ships as opt-in (`adv_participation_coeff=None` by default — zero behavior change for
every existing caller).** The realistic-scale result is the more informative one: for
this repo's typical use (liquid large/mid-cap universes, weekly-to-monthly rebalance,
research-scale notional), per-symbol ADV impact is a real but SMALL refinement on top of
the existing portfolio-turnover cost model — worth having for completeness and for anyone
backtesting a genuinely large or high-turnover strategy (where the stress-test scale shows
it matters a great deal), but not something that should be expected to dramatically move
every-day research results. `TASKS.md`'s calibration item (`adv_impact_coeff` — the
existing portfolio-level one — needs calibration against real slippage data, still
blocked) applies equally to this new coefficient: it remains a free parameter until real
execution slippage data exists to calibrate against.
