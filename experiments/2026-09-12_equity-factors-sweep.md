# Equity factor sweep: UMD momentum + KLN seasonalities (2026-09-12)

Companion to `experiments/2026-09-12_umd_equity-momentum.py` and
`experiments/2026-09-12_kln_seasonalities.py`. Bottom line first: **both
cross-sectional equity factors come out flat-to-negative on our in-store equity
universe — and the cause has been diagnosed as a data-universe defect, not a
market verdict.** Do NOT read these as "momentum/seasonality don't work."

## The headline finding: `yfinance_universe_prices` is missing the mega caps

The table is billed as Russell-3000 backfilled from the `securities` Iceberg
table (`is_russell3000=true`), but **285 of the 2,555 Russell-3000 names are
absent — and the missing names are almost all large caps**: AAPL, MSFT, AMZN,
NVDA, GOOG, GOOGL, JPM, WFC, BAC, XOM, CVX, PG, KO, DIS, UBER, etc. (the
backfill's own progress file: 2,285 done + 13 empty, fetched 2026-08-08 against
an earlier, smaller flag set). Result: the table is effectively a
**small/mid-cap slice**, and any cross-sectional factor backtest on it is
biased by (a) the missing mega caps and (b) aliveness/2026-08 snapshot
survivorship — documented in `evaluation/universe.py` for the `prices` table,
equally true here.

Consequence for the catalog: **no cross-sectional equity factor backtest is
trustworthy until the 285-name gap is backfilled** (TODO added to TASKS.md).
TSMOM/carry on the `futures` table are unaffected (separate data).

## UMD — cross-sectional momentum (12-1 / 6-1 / 12-0, deciles)

Construction: sign of 1-year return skipping the most recent month (and 6-1 /
12-0 variants), 1%/99% winsorize, deciles, long top/short bottom, month-end
rebalance, ~22-day hold, PIT-safe; eligibility close >= $5 and trailing-21d avg
dollar volume >= $10M. Sample: 21.8k stock-months, 1990-2026 (from ~70 names in
1990 to ~13-17k names in the 2020s).

| signal | Sharpe | ann. mean | t |
|---|---|---|---|
| r12-1 | -0.13 | -4.0% | -0.67 |
| r6-1 | -0.12 | -3.9% | -0.64 |
| r12-0 | -0.02 | -0.8% | -0.12 |

Decade r12-1 Sharpe: 1990s -1.8 / 2000s +0.27 / 2010s -0.35 / 2020s -0.22.
Rank IC mean -0.012 (t -0.9); the top decile even underperforms the equal-
weight market long-only. Daily 1-lag return autocorrelation is healthy
(t = -20.9), so the machinery is sound — the monthly cross-section is what's
flat. This is the classic survivorship + narrow-universe signature, not a
construction trace (e.g., June-2020's "top decile" is mREITs/energy crash-
rebound survivors, fine mechanically, un-representative of a broad market
momentum book).

## KLN — monthly return seasonalities

Signal = a stock's average return in a given calendar month over the past 1-10
years (excluding the current year); deciles; long top / short bottom; months
1995-2026 (~1,280 names/month).

| measure | value |
|---|---|
| Sharpe (L/S) | 0.01 |
| ann. mean | +2.2% |
| t | 0.88 |
| decades | 1990s 0.15 / 2000s 0.02 / 2010s 0.04 / 2020s -0.13 |

Same universe defect applies. A proper KLN-style test needs the full Russell
(and ideally delisting-inclusive) cross-section.

## What to do next (TASKS.md TODOs added)

1. **Backfill the missing 285 mega caps** into `yfinance_universe_prices` (re-run
   `yfinance_universe_backfill.py` seeded with `securities.is_russell3000`
   minus already-fetched, or fetch the 285 directly) and rerun both factors.
2. Keep the harness — `experiments/2026-09-12_umd_equity-momentum.py` is a
   template for any cross-sectional factor (rank IC, deciles, turnover, net
   grid).
3. In the meantime, the futures-side factor library (TSMOM + carry) is
   validated and takes priority in the catalog.