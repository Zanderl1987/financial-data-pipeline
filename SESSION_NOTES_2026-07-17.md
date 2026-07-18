# Session Notes — 2026-07-17

**Branch:** master
**Session model:** Claude Sonnet 5

## What happened

Resumed work on the TradingView Technical Rating replica. Located the existing pieces
(built in an earlier session, not previously session-noted):

- `tradingview_pipeline.py` — pulls TV's live Technical Rating gauge (Strong Buy/Buy/
  Neutral/Sell/Strong Sell) from the free scanner endpoint. Current-value only; TV has
  no history endpoint. Only 1 day of accumulated history so far
  (`storage/raw/tradingview/year=2026/month=07/tv_ratings_20260703.parquet`) — daily
  accumulator, wired into `run_all.py`.
- `analytics/technical.py` — local replica of TV's rating formula (`tv_rating()`,
  `rating_history()`, `rating_panel()`), reverse-engineered and verified against the live
  scanner (`rating_all == mean(rating_ma, rating_osc)`). Recomputes the rating from any
  OHLCV history, so it's fully backtestable even though the live pipeline itself has
  almost no accumulated history yet.

Zander wants to backtest this signal properly (IC, p-values, t-stats) and get an
interactive dashboard to evaluate whether it's worth building real trading logic on top
of it. Ran full `superpowers:brainstorming` (with `signal-eval` skill loaded for PIT/IC
methodology) to design it before writing any code.

## Design decisions (brainstorming session)

Spec written and committed: `docs/superpowers/specs/2026-07-17-tv-rating-backtest-dashboard-design.md`
(commit `837314e`).

- **Universe**: all 69 `tiingo_prices` symbols (mega-caps + sector/bond/commodity ETFs),
  full available history (as early as 1990 → 2026-07-15).
- **Methodology**: extends the existing `sentiment_eval.py` pattern rather than a new
  harness from scratch — pooled + daily cross-sectional Spearman IC with t-stats across
  1/3/5/10/21-day horizons, excess vs SPY, next-close entry (no same-day look-ahead).
  Evaluates **three** signals side by side: `rating_all`, `rating_ma`, `rating_osc`.
- **Also**: a rating-transition event study (which upgrade/downgrade types predict a move
  — extends the `event_backtest.py` pattern), and a rule-based trade simulation (long on
  cross into `strong_buy`, exit below buy zone; mirrored short on `strong_sell`; fixed
  $10k notional per trade; entry/exit on threshold *crosses*, not level checks).
- **Two-stage pipeline**: `tv_rating_eval.py` (compute, writes parquet/JSON to
  `storage/reports/tv_rating_eval/`) feeding `generate_tv_rating_report.py` (report only,
  never recomputes) — decouples the slow 36-year indicator computation from dashboard
  iteration.
- **Output**: single self-contained Plotly HTML (`storage/reports/tv_rating_backtest.html`),
  no server. 9 sections including a per-symbol price chart with trade markers (dropdown
  across all 69 symbols) and a cumulative realized-P&L chart with per-trade $ / % 
  annotations — explicitly a sum of independently-sized trades, not a capital-constrained
  portfolio curve (noted as a scope boundary, since trades can overlap in time).
- Neither script gets wired into `run_all.py`/`curated.py`/pipeline-catalog tests — this
  is an analysis tool over already-curated data, not a data-ingestion pipeline.
- Out of scope this round: daily refresh wiring, compounding/equity-curve sizing,
  transaction costs/slippage/borrow, any live/paper execution.

## State

Spec approved and committed. **Not yet implemented** — next step is
`superpowers:writing-plans` to turn the spec into an implementation plan, then build
`tv_rating_eval.py` + `generate_tv_rating_report.py` + the `tests/test_analytics.py`
additions (crossing-detection, P&L math, IC/spread stats on a synthetic panel).
