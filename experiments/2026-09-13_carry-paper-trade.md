# 10%-vol carry long-only forward paper trade -- armed

**Date:** 2026-09-13
**Run script:** `experiments/2026-09-13_carry-paper-trade.py`
**Builds on:** `experiments/2026-09-12_carry_futures.py` (published carry construction, imported by reference), `experiments/_tsmom_core.py` (roll handling + `portfolio_returns`)
**Artifacts:** `storage/reports/eval/carry_paper/book_state.json`, `storage/reports/eval/carry_paper/equity_daily.parquet`, `storage/reports/eval/carry_paper/position_current.parquet`
**Run:** `C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_carry-paper-trade.py` (full repro + arm) / `--daily` (refresh, scheduler-able)

## Bottom line

The published **carry long-only** book is now a forward paper trade, armed today at a
**10% annualized book-vol footprint**. Guard reproduction of the published construction
is exact: gross Sharpe **0.46**, net-10bps **0.43** on the full 2002-05-01 -> 2026-09-11
history. Live state at arming: the book in force through 2026-09-11 carries **21
instruments** from the 2026-08-31 signal; the next rebalance (provisional anchor
2026-09-11) deploys **2026-10-01** at month end per the backtest's convention. Official
live start is **2026-09-14** (next trading day after data), so zero forward P&L rows
accrue until the store advances past 09-11.

The weekly/daily refresh command is ready (`--daily`); **actually scheduling it** (Task
Scheduler cadence) is Zander's operational call, which is all this queue item was waiting
on. State is kept out of the shared evaluation registry by the family-2 rule.

## Construction (all fixed, none tuned here)

- Signal: `build_carry` (roll-gap proxy, min_events=3), positions long-only above the
  cross-sectional median, per-instrument ex-ante vol target 0.40 with floor 0.10,
  positions effective t0+1, roll-day returns excised -- all **imported verbatim** from
  the published 2026-09-12 module. The backtest's gross/net came back identical, so the
  reuse is genuine, not recomputed.
- Book-footprint scaling: daily portfolio P&L x target_book_vol / rolling-252 vol
  (shifted 1d, floor 5%) -- same PIT scaler the VTSL module uses. The 10% is a *book
  footprint*, not a search parameter.

## Forward-book deviations (pre-registered in the script docstring, all three stated)

1. **Robust-vol sizing (min_periods=200) for the forward book only.** The published
   estimator uses `rolling(252).std()` exact-window; ANY NaN inside the trailing 252 days
   zeroes that symbol's vol and its position. That is harmless at true month-ends but the
   live store carries cross-market calendar rows (e.g. 2026-09-06/07/08) where most
   contracts are NaN, so every anchor within ~252 days of such a row sees 0/44 finite vol
   and a flat book. The forward book sizes with `rolling(252, min_periods=200)`, identical
   on dense months. Measured delta at the current anchor: 21 names (robust) vs 15
   (published) -- the 6 rescued names were calendar-rows, not the signal. The warm-up guard
   uses the UNTOUCHED published function and still reproduces 0.46/0.43.
2. **Completed-month anchoring.** `month_end_anchors` labels each month's anchor with its
   last actual trading day; in real-time the current month's anchor is computed from
   partial data and `shift(1)` would deploy it mid-month. The forward book only holds
   anchors whose calendar month has fully elapsed; the provisional anchor is reported
   pending and deploys at month end (the only way the backtest ever deployed).
3. **No registry writes.** Futures-side results are family-2; forward state lives in the
   carry_paper artifact dir.

## State at arming

- Data through 2026-09-11; live signal date 2026-08-31; 21 instruments.
- Pending rebalance: 21 instruments from the 2026-09-11 provisional anchor, deploys
  2026-10-01.
- `equity_daily.parquet`: warm-up history (published-construction gross, net-10bps,
  10%-scaled equity) + forward book (completed-month anchors, `fwd_return`/`fwd_equity`
  only populated from 2026-09-14 onward, `pre_arm_backfill` flags the deterministic
  pre-arm rows).

## Caveats

- Carry is the roll-gap proxy from the 2026-09-12 build (front-month-only store, no
  F1/F2); rank signals, not absolute levels.
- The "0.33 net" figure cited for this book earlier was on a per-rebalance cost basis;
  the engine's turnover-proportional 10bps net here is 0.43. Ingestion convention, not a
  signal difference.
- Forward P&L will only start accruing when the futures store advances past 2026-09-11;
  until then `fwd_return` is empty (0 forward days) -- expected for a book armed today.

## Next

- Zander decides whether to schedule `--daily` (Task Scheduler) for real daily cadence.
- Optional: add a carry-paper panel to the dashboard UI reading `carry_paper/` artifacts
  (mirrors the TV daily-paper-trade panel pattern).