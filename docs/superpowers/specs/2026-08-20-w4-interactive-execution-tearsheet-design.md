# W4 — Interactive Execution & Tearsheet Layer — Design

Date: 2026-08-20
Status: Approved

## Goal

`backtest_app.py` (the live Dash explorer from the 2026-08-03 spec) lets a user
drag entry/exit threshold sliders and see trades update live, but it predates
the W1 engine unification: it never passes an `ExecutionConfig` into
`evaluation.trades.simulate()`, so there are no cost/risk/sizing controls, and
every live run is effectively `config=None` (LEGACY: no costs, no stops,
unlimited concurrency). Separately, W3 built `evaluation/tearsheet.py` with
its compute/render split specifically so "W4's Dash callbacks call the same
functions the static HTML does" (2026-08-17 session notes) — but nothing live
consumes it yet; tearsheet output is still static-HTML-only.

This spec closes both gaps: wire the `ExecutionConfig` groups into
`backtest_app.py` as live controls, and add a live tearsheet section fed by
the same simulated trades.

## Context

- `evaluation/execution.py::ExecutionConfig` — frozen dataclass, four groups
  (`costs: CostModel`, `risk: RiskControls`, `sizing: Sizing`,
  `limits: PortfolioLimits`), each already grouped "because W4's sliders map
  onto exactly these groups" (2026-08-16 design doc). `resolve(config)` treats
  `None` as `LEGACY` (today's behavior, unchanged).
- `evaluation/trades.py::simulate(rule, cache, notional=None, *, config=None)`
  and `simulate_symbol(..., *, config=None)` already accept a config
  (Step B, commit `115b652`) — this spec is the first caller to pass a
  non-`None` one from the live app.
- `evaluation/tearsheet.py::daily_returns_from_trades(trades)` bridges a
  realized-trades DataFrame to a daily return series (`basis="realized"`,
  lower-bound drawdown, non-comparable Sharpe to mark-to-market — documented
  caveat, unchanged here). `tearsheet.tearsheet(returns, bench_returns=None)`
  computes headline metrics, monthly-returns table, rolling metrics, and
  drawdown periods from that series.
- `generate_tearsheet.py` holds the Plotly figure-builder functions for those
  same metric dicts (monthly heatmap, rolling-metrics chart, drawdown table,
  headline tiles) — reused directly by the new callbacks, not reimplemented.
- `backtest_app.py`'s existing `_SIM_CACHE`, `has_trade_rule()`,
  `KNOWN_TRADE_RULE_SIGNALS` gating, and `_slider()` helper are unchanged and
  reused as-is.
- `evaluation/execution.py::config_hash(config)` — stable 12-hex digest of a
  config's semantics, already built for registry rows; reused here as a cache
  key component.

## Architecture & data flow

```
Threshold sliders (existing) ──┐
                                 ├─> TradeRule
Execution Config panel (new) ──┼─> ExecutionConfig
  Costs / Risk / Sizing / Limits┘
        │
        ▼
  evaluation.trades.simulate(rule, cache, config=cfg)   [existing engine,
        │                                                 first live caller
        │                                                 with config != None]
        ▼
  trades: pd.DataFrame
        │
        ├──> existing trade-summary / symbol-fig / P&L-fig panels (unchanged)
        │
        └──> evaluation.tearsheet.daily_returns_from_trades(trades)
                   │
                   ▼
             evaluation.tearsheet.tearsheet(returns)
                   │
                   ▼
             generate_tearsheet.py figure builders   [reused, not duplicated]
                   │
                   ▼
             new "Tearsheet" panel: headline tiles, monthly heatmap,
             rolling Sharpe/Sortino, drawdown table
```

`_SIM_CACHE`'s key extends from
`(name, run_id, bull_min, exit_long_max, bear_max, exit_short_min)` to also
include `ev_execution.config_hash(cfg)`, so two different execution configs
against the same thresholds don't collide in the memo.

## Execution Config panel

One collapsible sub-section per dataclass group, mirroring the group
boundaries in `execution.py` directly (no new grouping invented):

- **Costs** — `commission_bps`, `spread_bps`, `borrow_fee_bps`: sliders,
  range 0-50 (bps), default 0 (matches `CostModel()` defaults). `impact_model`:
  dropdown `None | "sqrt" | "flat"`. `impact_coeff`: slider, enabled only when
  `impact_model` is not `None`.
- **Risk** — `stop_loss_pct`, `take_profit_pct`, `vol_stop_mult`: numeric
  inputs, blank = `None` (matches dataclass default). `trailing`: checkbox.
  `max_holding_days`: numeric input, blank = `None`.
- **Sizing** — `mode`: dropdown `fixed_notional | fixed_fraction`.
  `notional`: numeric input (shown/used for `fixed_notional`). `fraction`:
  numeric input, 0-1 (shown/used for `fixed_fraction`). `max_weight`: numeric
  input, blank = `None` (not consumed by the discrete-trade engine but kept
  for config completeness/round-tripping).
- **Limits** — `capital`, `max_concurrent`, `max_drawdown_stop`: numeric
  inputs, blank = `None`.

Each callback fire rebuilds `ExecutionConfig(costs=CostModel(...),
risk=RiskControls(...), sizing=Sizing(...), limits=PortfolioLimits(...))`
from current control values. All controls use `updatemode="mouseup"` for
sliders (matching the existing threshold sliders) so simulation runs once per
adjustment, not once per drag pixel.

## Tearsheet panel

New layout section below the existing P&L figure. Renders, from
`tearsheet.tearsheet(returns)`'s output via `generate_tearsheet.py`'s
builders:

- Headline metric tiles (CAGR, Sharpe, max drawdown, % positive months).
- Monthly-returns heatmap.
- Rolling Sharpe/Sortino/vol chart.
- Drawdown-periods table.

No benchmark overlay in this round — `backtest_app.py` has no benchmark
return series wired in today, and adding one is a separate scoping decision.
`tearsheet()`'s `bench_returns` stays `None`; `benchmark_stats` output (and
its panel) simply doesn't render.

## Error handling & robustness

- **Invalid execution config** (e.g. `sizing.mode="fixed_fraction"` with no
  `limits.capital`, or a negative cost field) — `ExecutionConfig`'s
  `__post_init__` already raises `ValueError` on construction. The callback
  catches it and shows the message inline (e.g. "mode='fixed_fraction'
  requires limits.capital") instead of crashing the panel or falling back
  silently to a different config.
- **Zero realized trades** — tearsheet panels show "no realized trades to
  compute tearsheet", reusing `daily_returns_from_trades`'s existing
  `{"returns": None, "returns_reason": ...}` path; no new empty-state logic
  needed beyond checking that key.
- **No `TradeRule` for this signal** — Execution Config panel and Tearsheet
  section stay disabled, same `has_trade_rule()` gating the rest of the app
  already uses; no separate check invented.
- **Degenerate low-variance returns** — already handled inside
  `tearsheet.py` (`SD_FLOOR`), unchanged here.

## Testing

- New tests for `backtest_app.py`'s own logic, as plain functions the
  callbacks wrap (existing pattern, no Selenium/browser harness):
  - Building an `ExecutionConfig` from a set of control values.
  - Invalid-config values produce the expected inline error message, not an
    unhandled exception.
  - `_SIM_CACHE` key includes `config_hash`, so two configs against identical
    thresholds don't share a cache entry (and identical configs do).
  - Tearsheet happy path: non-empty trades → non-`None` figures for all four
    panels.
  - Tearsheet zero-trades path: empty trades → "no realized trades" message,
    no figures, no exception.
- `evaluation/tearsheet.py`, `evaluation/execution.py`, and
  `evaluation/trades.py`'s `config=` handling already have their own test
  coverage (W1/W3) — untouched, no new tests there.

## Out of scope (this round)

- Benchmark overlay in the live tearsheet (`bench_returns`).
- W5 (optimizer).
- Retiring or modifying `generate_eval_report.py` / `generate_tearsheet.py`'s
  static-HTML paths — both stay as the portable/archival output; this app
  remains the day-to-day live exploration tool.
- Wiring `backtest_app.py` into `run_all.py` or any automation — analysis
  tool over already-curated/already-evaluated data, same boundary as today.
- `sizing.max_weight` — carried in the config for completeness but not
  consumed by the discrete-trade engine (matches `execution.py`'s own
  docstring: it's the weight-matrix engine's field).
