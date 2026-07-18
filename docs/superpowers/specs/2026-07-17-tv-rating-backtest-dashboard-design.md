# TradingView Rating Backtest + Interactive Dashboard — Design

Date: 2026-07-17
Status: Approved

## Goal

Evaluate whether the local TradingView Technical Rating replica (`analytics/technical.py`
`tv_rating()`/`rating_panel()`) actually predicts forward returns, with proper statistical
rigor (IC, t-stats, p-values), and produce an interactive HTML report to visualize the
results — including a simple rule-based trade simulation — as a first look at whether
real trading logic could be built on top of this signal.

## Data & point-in-time discipline

- **Source**: `analytics.technical.rating_panel()` computed from `tiingo_prices` (via the
  curated query layer, never raw globs), all 69 available symbols, full available history
  per symbol (as early as 1990, through the latest curated data, currently 2026-07-15).
- **Universe caveat**: fixed 69-symbol list — mega-cap stocks + sector/bond/commodity ETFs,
  not a broad-market sample. No survivorship-bias concern (static list, not "today's index
  membership"), but the report must state this caveat explicitly rather than implying
  general market coverage.
- **Signal timing**: rating for day T is computed from day T's OHLCV close (no publication
  lag to model — it's derived directly from the same closing price, not a delayed external
  filing).
- **Entry timing**: next trading day's close, minimum — matching `sentiment_eval.py`'s
  convention. Same-day entry is not permitted.
- **Returns**: excess vs. SPY (SPY excluded from its own benchmark comparison).

## Stage 1 — `tv_rating_eval.py` (compute)

Root-level script, sibling to `sentiment_eval.py` / `event_backtest.py`. Not wired into
`run_all.py`, `curated.py`, or the pipeline-catalog tests — this is an analysis tool over
already-curated data, not a data-ingestion pipeline.

### Level-IC evaluation (extends the `sentiment_eval.py` pattern)

For each of three signals — `rating_all`, `rating_ma`, `rating_osc` — and five horizons
(1/3/5/10/21 trading days):

- Pooled Spearman IC + p-value (all symbol-day pairs).
- Mean daily cross-sectional IC + t-stat (days with ≥5 symbols carrying a non-null rating
  that day).
- Bucket spread: mean forward excess return for `strong_buy` vs `strong_sell` days, with a
  Welch's t-test p-value on the spread.

Same reporting shape as `sentiment_eval.py`'s `evaluate()`, generalized to loop over the
three signal columns.

### Transition event study (extends the `event_backtest.py` pattern)

- Detect `rating_label` transitions per symbol (e.g. `neutral→buy`, `buy→strong_buy`, any
  upgrade/downgrade pair).
- For each transition type, compute the average cumulative forward-return path from day 0
  through day +21 across all occurrences of that transition, plus n and a t-stat on the
  day-21 cumulative return against zero.

### Trade simulation

Rule-based long/short simulation driven by `rating_all` only:

- **Long entry**: `rating_all` crosses up through +0.5 (into `strong_buy`).
- **Long exit**: `rating_all` drops below +0.1 (out of buy territory).
- **Short entry**: `rating_all` crosses down through −0.5 (into `strong_sell`).
- **Short exit**: `rating_all` rises above −0.1 (out of sell territory).
- One position per symbol at a time — no pyramiding, no scaling in/out.
- Trigger on **crossing** the threshold, not merely "currently beyond it" (must compare
  against the prior day's value to detect the cross, not just a level check).
- **Sizing**: fixed $10,000 notional per trade. P&L = `10000 * (exit/entry - 1)` for longs,
  `10000 * (1 - exit/entry)` for shorts, computed off adjusted close.
- Entry/exit prices use the same next-close timing as the IC evaluation (signal on day T →
  position opens at T+1 close).

### Output artifacts (`storage/reports/tv_rating_eval/`)

- `ic_stats.json` — the IC/spread table (signal × horizon).
- `transitions.parquet` — per-transition-type average return paths.
- `panel.parquet` — full symbol-day signal+return panel (lets the report stage build the
  scatter/histogram without recomputing indicators).
- `trades.parquet` — one row per simulated trade: symbol, side, entry_date, entry_price,
  exit_date, exit_price, pnl_dollars, pnl_pct.

## Stage 2 — `generate_tv_rating_report.py` (report)

Reads Stage 1's output files only — never recomputes indicators. Writes a single
self-contained HTML file (`storage/reports/tv_rating_backtest.html`) with embedded
Plotly.js (all data inlined, no server, no external requests). Sections:

1. **Headline stats table** — IC / t-stat / spread for all 3 signals × 5 horizons,
   color-coded against skepticism-default thresholds (grey = noise, yellow = weak,
   green = significant).
2. **IC bar chart** — mean daily IC by horizon, grouped by signal, with t-stat error bars.
3. **Bucket spread chart** — bullish vs. bearish mean excess return by horizon, per signal.
4. **Rating-vs-forward-return scatter** — signal/horizon picker, hover shows symbol/date.
5. **Transition event-study chart** — average cumulative return path (day 0→21) per
   transition type, toggleable lines.
6. **Per-symbol table** — n signals, best/worst horizon IC, sortable (spot-checks whether
   any effect concentrates in a few tickers rather than being broad-based).
7. **Price + trades chart** — symbol dropdown across all 69 symbols; price line with
   entry/exit markers (▲ long entry, ▼ short entry, × exit), colored by realized win/loss.
8. **Cumulative P&L chart** — running sum of realized trade P&L in chronological order (by
   exit date) across all symbols/trades. This is a sum of independently-sized $10k trades,
   **not** a capital-constrained portfolio equity curve — trades can overlap in time and
   don't draw from a shared capital pool. Each realized trade is annotated at its exit
   point with its $ P&L and % P&L.
9. A short "how to read this" panel restating the skepticism defaults (|IC| < 0.02 noise,
   t ≥ 2 over ≥ ~250 days for significance, sign flips across horizons = noise not
   momentum-then-reversal) so the report doesn't get over-read.

## Testing

Added to the existing `tests/test_analytics.py` (no new test file):

- Crossing-detection logic fires only on an actual threshold cross (prior value on one
  side, current value on the other), not merely "currently beyond the threshold."
- P&L math verified against a hand-computed long trade and a hand-computed short trade.
- IC/spread stat functions verified on a small synthetic panel with a known planted
  correlation.

## Verification plan

1. Run `tv_rating_eval.py` against real curated data (69 symbols, full history); confirm
   the printed stats table matches the `sentiment_eval.py`-style format and sane ranges.
2. Spot-check one symbol's simulated trade log by eye against its own price chart.
3. Run `generate_tv_rating_report.py` and open the resulting HTML in a real browser —
   confirm every chart renders and the symbol picker / toggles work, not just that the
   script exited 0.
4. Walk the point-in-time checklist explicitly: join lag (n/a — same-day close), entry
   timing (next close, confirmed), excess-vs-benchmark (SPY, confirmed), universe caveat
   stated in the report text.

## Out of scope (this round)

- Wiring into `run_all.py` as a daily-refreshing pipeline.
- Position sizing beyond fixed $10k notional (no compounding/equity-curve simulation).
- Transaction costs, slippage, or borrow costs for the short side.
- Any live/paper trading execution — this is evaluation only.
