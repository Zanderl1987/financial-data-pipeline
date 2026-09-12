# Equity factor sweep: UMD momentum + KLN seasonalities (2026-09-12)

Companion to `experiments/2026-09-12_umd_equity-momentum.py` and
`experiments/2026-09-12_kln_seasonalities.py`. Bottom line: **both cross-sectional
equity factors are flat-to-negative in our in-house equal-weight decile construction,
and that holds on BOTH the original (broken) universe and the full universe after the
285-name mega-cap backfill.** The first pass looked like a pure data artifact; the
re-run after closing the hole proves it is a genuine null in this universe/spec — with
one remaining caveat (alive-2026 survivorship compresses the short leg; see below).

## Update history

| date | universe | UMD r12-1 Sharpe | KLN Sharpe |
|---|---|---|---|
| 2026-09-12 (run 1) | 2,285 names — 285 mega caps MISSING | -0.13 (t -0.67) | 0.01 (t 0.88) |
| 2026-09-12 (run 2) | 2,570 names — hole CLOSED | -0.11 (t -0.62) | -0.00 (t -0.25) |

## Act 1: `yfinance_universe_prices` was missing the mega caps

The table is billed as Russell-3000 backfilled from the `securities` Iceberg
table (`is_russell3000=true`), but **285 of the 2,555 Russell-3000 names were
absent — and the missing names were almost all large caps**: AAPL, MSFT, AMZN,
NVDA, GOOG, GOOGL, JPM, WFC, BAC, XOM, CVX, PG, KO, DIS, UBER, etc. (the
backfill's progress file: 2,285 done + 13 empty, fetched 2026-08-08 against an
earlier, smaller flag set). Result: the table was effectively a
**small/mid-cap slice**.

## Act 2: the fix — 285-name backfill (later on 2026-09-12)

Ran `yfinance_universe_backfill.py`, which resumes against the *current*
`securities.is_russell3000` flag set: **285 names fetched in 6 bulk yf.download()
batches** (`yfinance_universe_batch0047..0052_20260912.parquet`, ~2.4M rows),
then `curated.py` rebuilt the table. After: **2,570 symbols, ZERO missing vs the
Russell-3000 flag set** (verified with the same query the harness uses).
Also fixed a latent tool bug: the resume logic treated the progress file's
`empty` list as terminal, so a transient empty fetch permanently poisoned a
symbol — `empty` is now retried every run (the 2026-08-08 run had wrongly
marked THO/TREX/SMG/TMDX etc. as empty). Those 13 still return nothing on
retry in 2026-09-12 (likely delisted-ticker/symbol-form issues, not transient).

## UMD — cross-sectional momentum (12-1 / 6-1 / 12-0, deciles)

Construction: sign of 1-year return skipping the most recent month (and 6-1 /
12-0 variants), 1%/99% winsorize, deciles, long top/short bottom, month-end
rebalance, ~22-day hold, PIT-safe; eligibility close >= $5 and trailing-21d avg
dollar volume >= $10M. Sample (run 2): 287k eligible stock-months, 1990-2026.

| signal | Sharpe | ann. mean | t |
|---|---|---|---|
| r12-1 | -0.11 | -3.6% | -0.62 |
| r6-1 | -0.05 | -1.6% | -0.30 |
| r12-0 | -0.03 | -1.0% | -0.17 |

Decade r12-1 Sharpe (run 2): 1990s -0.83 / 2000s +0.20 / 2010s -0.30 / 2020s -0.15
(run 1 was 1990s -1.8 / 2000s +0.27 / 2010s -0.35 / 2020s -0.22 — statistically
indistinguishable). Rank-IC-decile slope -2.1 bp/decile. The machinery is sound
(daily 1-lag return autocorr t = -20.9; net-of-cost grid keeps the null flat to
-0.19); **equal-weight decile momentum is simply not there in this sample.**
Note both long AND short deciles returned ~+29-31%/yr gross — this is the
alive-2026-snapshot signature on an equal-weight basis; every decile drifted up.
That survivorship is precisely what inflates the short-leg return and shrinks
the L/S spread vs a delisting-inclusive reality.

## KLN — monthly return seasonalities

Signal = a stock's average return in a given calendar month over the past 1-10
years (excluding the current year); deciles; long top / short bottom; months
1995-2026 (~1,500 names/month in run 2).

| measure | run 1 | run 2 |
|---|---|---|
| Sharpe (L/S) | 0.01 | -0.00 |
| ann. mean | +2.2% | -1.4% |
| t | 0.88 | -0.25 |
| decades | 0.15/0.02/0.04/-0.13 | 0.17/0.03/0.05/-0.05 |

Same story: adding the 285 names changed nothing material. Flat.

## What to do next (TASKS.md TODOs added)

1. **Done:** backfill the missing 285 mega caps (2026-09-12) — the unblock. Both
   factors were re-run and remain null on the full universe.
2. **Open caveat — survivorship.** `yfinance_universe_prices` is an alive-2026
   snapshot (delisted names absent; documented for `prices` in
   `evaluation/universe.py`). Momentum's classic profit comes disproportionately
   from shorting recent losers that go on to delist; an alive-only panel
   understates it. A delisting-inclusive panel (e.g., Sharadar SEP
   `delisting_reference` permaticker keys + the corrected `prices` back-adjust)
   is the honest upgrade path before declaring momentum dead in this market.
3. Keep the harness — `experiments/2026-09-12_umd_equity-momentum.py` is a
   template for any cross-sectional factor (rank IC, deciles, turnover, net grid).
4. The futures-side factor library (TSMOM + carry) remains validated and takes
   priority in the catalog.