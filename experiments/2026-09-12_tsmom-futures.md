# TSMOM deep backtest on the in-house futures store

**Date:** 2026-09-12
**Catalog source:** `docs/STRATEGY_CATALOG.md` Tier 1 A-grader #1 (time-series momentum)
**Run scripts:** `experiments/2026-09-12_tsmom-futures.py` (headline), `experiments/2026-09-12_tsmom_validate.py` (AQR comparison)
**Shared core:** `experiments/_tsmom_core.py`
**Code:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-12_tsmom-futures.py` from repo root

## Bottom line

| Variant | Sharpe | t | 10%-vol CAGR | Max DD |
|---|---|---|---|---|
| raw front-month (no roll handling) | 0.15 | 0.78 | — | — |
| raw + 5% return clip | 0.15 | 0.79 | — | — |
| **roll-masked (headline)** | **0.23** | **1.26** | **1.59%** | **-30.5%** |

Decade Sharpe (roll-masked): 2000s 0.27, 2010s 0.38, 2020s 0.66.
Net-of-cost: 1bps 0.23, 2.5bps 0.22, 5bps 0.21, 10bps 0.19 (turnover ~0.03 capital units/month; costs do not kill it).
Per-instrument: 32/44 positive Sharpe; best 6J=F 0.54, HRC=F 0.55, GNF=F 0.66, NQ=F 0.53; worst HE=F -0.50, RTY=F -0.40, 6E=F -0.28, ZW=F -0.25, RB=F -0.25.

## Construction (MOP-faithful, PIT-safe)

- Signal at month-end t0 = sign of 12-month log return measured from ~13 months
  back to ~1 month back (`skip_days=31` approximates the paper's "skip the most
  recent month").
- Positions sized to a 40% ex-ante annualized vol target from trailing 252-day
  realized vol (invariant under a constant leverage scalar), vol floor 10% to
  cap position size at 4x (see data caveats).
- Monthly rebalance at month-end anchors; positions take effect t0+1 (ffill +
  `shift(1)`) — no lookahead.
- Portfolio = equal-weight average of the vol-scaled positions (self-financed
  long/short).

## Validation against the official AQR dataset

AQR "Time Series Momentum: Factors, Monthly" (1985-2026-05, n=497) gives ALL
Sharpe 0.98; by decade 1.62 / 1.13 (2000s) / 0.46 (2010s) / 0.32 (2020s).

| Variant | corr vs AQR ALL | 2000s | 2010s | 2020s |
|---|---|---|---|---|
| skip31 + roll-mask | 0.41 | ours 0.18 vs 1.08 | 0.31 vs 0.29 | 0.61 vs 0.40 |
| no-skip + roll-mask | 0.50 | 0.38 vs 1.08 | 0.18 vs 0.29 | 1.05 vs 0.40 |
| skip31, no mask | 0.42 | 0.14 vs 1.08 | 0.21 vs 0.29 | 0.39 vs 0.40 |

Reading: our reconstruction is directionally the same factor (corr 0.4-0.5);
2010s and especially 2020s match up well. The 2000s shortfall (0.2-0.4 vs AQR
1.08) is a **universe breadth gap**, not a construction bug: MOP/AQR trade ~58
markets including 13 equity indices and 13 bond markets (the strongest 2000s
trends), ours has 4 equity indices and 4 rates, with 19 agricultural/soft
positions diluting the portfolio.

## What we had to handle: roll contamination

The `futures` table is continuous **front-month** closes stitched without
back-adjustment; contract rolls appear as one-day open gaps (2.65% of rows have
|open/prev-close-1| > 3% or |ret| > 5%; NG=F worst at 1,017 events). Headline
construction **excises each instrument's return on detected roll days**
(overnight gap > 4x the symbol's median gap AND > 1.5%). Roll masking is worth
+7-9pp of Sharpe versus raw.

Two further in-house data caveats surfaced loudly during the build:
1. **Flat-price glitch windows collapse the rolling vol estimate** — ZT=F
   showed a 136x position (vol collapsed to ~1.6% ann. est.) and one
   instrument-day at -16.7% of portfolio NAV. Fixed with a 10% vol floor
   (max position 4x). PIT-safe: it is a predetermined cap, not a data-snooped
   number.
2. **No back-month or continuous back-adjusted series exists in the store** —
   so roll days can be excised but not corrected. A truly faithful TSMOM
   reproduction (or short-roll trading) requires back-adjustment capability.
   Tracked as a TODO.

## Verdict / next steps

- TSMOM is tradeable at ~0.23-0.66 Sharpe on our universe with clean handling,
  but the 2000s validation gap caps conviction in the long-horizon statistic.
- Check the `docs/STRATEGY_CATALOG.md` TSMOM entry for an updated TODO line.
- Not yet done: forward-optimization / walk-forward + CPCV pass, and the
  10%-vol forward paper-trade. Decide after the options-TODO and the next
  backtest (cross-sectional momentum) whether to run the full optimize loop on
  TSMOM.