# Oil-shock reaction factor: a same-day artifact, not a tradeable signal

**Repo:** financial-data-pipeline · **Date:** 2026-07-07 · **Status:** concluded (negative, reversing an earlier positive finding)

## Claim

A point-in-time event study of how oil-exposed stocks react to oil price shocks
(±15% moves over 10 trading days) initially showed a strong, statistically
significant co-movement effect for positively-exposed names (date-level t = 4.13,
BH-adjusted p = 0.0008 at the 1-day horizon). That result does not survive fixing
a look-ahead bug in the entry timing: once entry is moved to the next trading
day's close (the earliest point the shock is actually knowable), the effect
disappears entirely — every horizon in both exposure groups and both shock
directions clears no significance bar, and 0 of 9 (threshold, lookback-window)
sensitivity-grid combinations remain significant. The `oil_shock` factor (weight
0.5 in `signal_panel()`'s composite) has been removed.

## Motivation

`analytics/event_impact.py` (built earlier the same day) generalizes
`event_backtest.py`'s oil→airlines example into a repeatable driver-shock
research tool: classify symbols as positively/negatively oil-exposed using only
a trailing window known as of each event date, then run separate event studies
per exposure group. The positive-exposure leg's short-horizon (1-3 day) result
looked real and PIT-safe enough to wire into the live factor panel at a reduced
weight. Before trusting it further, a follow-up robustness pass added p-values,
a Benjamini-Hochberg multiple-comparisons correction, and a `(min_t,
lookback_years)` sensitivity grid — all of which the original result passed
cleanly (9/9 grid combinations significant and sign-stable). That clean pass is
what prompted a closer look at entry timing, which is where the bug was found.

## Data

- **Driver:** oil (WTI proxy via `analytics/exposure.py`'s `DRIVERS["oil"]`,
  trigger symbol USO), market control `spx`.
- **Universe:** `tiingo_pipeline.DEFAULT_SYMBOLS` (default watchlist).
- **Events:** `event_backtest.price_move_events("USO", pct=±15, days=10,
  min_gap_days=10)` — 44 surge episodes, 40 drop episodes classified with
  qualifying exposed symbols, full available history.
- **Exposure classification:** `_rolling_grouping()`, 3-year trailing window,
  `|t_ex_mkt| > 3.0` (both defaults).

## Method

1. **The bug.** `price_move_events()` defines an event's date as the day the
   trailing N-day percent move *itself closes* past the threshold — i.e. the
   date is only knowable as of that day's close. `driver_event_study()` called
   `event_backtest.event_study(..., entry_lag=0)` (the function's own default),
   which enters at that same close. This is exactly the failure mode the
   `signal-eval` skill calls the single most common look-ahead bug: trading on
   same-day information.
2. **The fix.** Added an `entry_lag` parameter to `driver_event_study()` and
   `sensitivity_check()`, defaulting to 1 (next close), and reran the full
   report and sensitivity grid at both `entry_lag=0` (the original, biased
   version, kept only for comparison) and `entry_lag=1` (corrected).
3. Same date-clustering-honest statistics as before: one mean CAR per event
   date, two-tailed t-test (df = n_dates − 1), BH-adjusted across the 5
   horizons tested per run.

## Results

**Positive-exposure leg, oil surge (44 event dates), date-level stats:**

| horizon | entry_lag=0 (biased) mean / t / p_adj | entry_lag=1 (fixed) mean / t / p_adj |
|---|---|---|
| 1 | +0.90% / t=4.13 / **p_adj=0.0008** | -0.23% / t=-1.25 / p_adj=0.476 |
| 3 | +0.88% / t=2.80 / **p_adj=0.0188** | -0.21% / t=-0.56 / p_adj=0.706 |
| 5 | +0.54% / t=1.43 / p_adj=0.269 | -0.59% / t=-1.26 / p_adj=0.476 |
| 10 | +0.49% / t=0.87 / p_adj=0.488 | -0.24% / t=-0.38 / p_adj=0.706 |
| 21 | +0.06% / t=0.08 / p_adj=0.937 | -0.88% / t=-1.08 / p_adj=0.476 |

**Negative-exposure leg, oil surge (39-40 event dates):** entry_lag=0 showed a
lone significant cell (h21: t=-2.86, p_adj=0.0342) — same-signed with the
"reaction" story but on the wrong horizon to trust (the report's own guide text
flags single-horizon, single-group significance as more likely confounding than
real). At entry_lag=1 this also drops (h21: t=-2.36, p_adj=0.1188).

**Oil drop direction (`--pct -15`, entry_lag=1, both legs, 37-40 dates):** no
horizon in either exposure group clears p_adj < 0.05 (best case h5 positive-
exposure: t=1.53, p_adj=0.393).

**Sensitivity grid** (surge, positive-exposure leg, h3, entry_lag=1, 9
combinations of `min_t ∈ {2.5,3.0,3.5} × lookback_years ∈ {2,3,5}`): all 9 agree
on sign (now negative, reversed from the original positive), mean CAR ranges
-0.02% to -0.21%, t ranges -0.06 to -0.56, **0/9 clear p_adj < 0.05** (vs. 9/9
before the fix). This is a clean, uniform null — not one lucky threshold driving
the reversal.

## Limitations & threats to validity

- **This is a negative result about one driver, one universe, one exposure
  method** — it does not rule out oil-shock reactions existing under a
  different classification or a longer/shorter reaction window; those weren't
  swept.
- **Sample depth.** 44 surge / 40 drop independent event dates over the
  available history is thin; a real small effect could still be hiding under
  this noise floor. Absence of significance here is evidence of absence at
  this sample size, not proof of a truly zero effect.
- **Benchmark.** SPY only; a sector-relative benchmark (e.g. XLE) wasn't tried
  and might change the picture for the positive-exposure (energy-heavy) leg.
- **entry_lag=1 is the minimum honest lag, not necessarily the realistic one**
  — real execution against a shock confirmed only at that day's close would
  likely need an even later entry once slippage/liquidity are modeled.

## Decision & next step

Removed `oil_shock` from `analytics/signals.py`'s `DEFAULT_WEIGHTS` and
`_raw_signals()` (the `oil_shock()` wrapper function was also deleted; the
underlying `analytics/event_impact.py` module is untouched and still usable as
a research tool with the corrected `entry_lag` default). `driver_event_study()`
and `sensitivity_check()` now default to `entry_lag=1`; pass `entry_lag=0`
explicitly only to reproduce the biased version for comparison. Any future
driver (gold, t10y, etc. — the framework already supports them via
`analytics/exposure.py`'s `DRIVERS`) must be validated with `entry_lag>=1`
before being considered for `signal_panel()`.

## Reproduce

```
# from repo root, C:\ProgramData\anaconda3\python.exe on all commands
python -m analytics.event_impact --driver oil --pct 15 --days 10 --entry-lag 0   # biased (for comparison)
python -m analytics.event_impact --driver oil --pct 15 --days 10                 # fixed (entry_lag=1 default)
python -m analytics.event_impact --driver oil --pct -15 --days 10                # drop direction
python -m analytics.event_impact --driver oil --pct 15 --days 10 --sensitivity   # threshold grid
python -m pytest tests/test_event_impact.py tests/test_signals.py -v
```
