# Interactive Backtest Explorer — Design

Date: 2026-08-03
Status: Approved

## Goal

Add a live, interactive GUI for exploring the results of the unified evaluation
framework (`evaluate.py` / `evaluation/`) — live parameter tuning of trade
rules, cross-filtered charts, and a per-symbol trade explorer — on top of the
existing statistical engine, which is already complete and acceptance-tested
(2026-07-22, commit `6478299`). The current reporting layer
(`generate_eval_report.py`) is a static, self-contained Plotly HTML file: solid
for archiving/sharing a run, but not explorable — no live parameter tuning, no
cross-filtering, no symbol-level drill-down. This spec covers a new live app
that fills that gap.

## Context

- `evaluate.py` + `evaluation/` (contracts, adapters, stats, runner, trades,
  registry) is the compute engine: PIT-safe, 3-tier significance battery,
  results recorded to `storage/eval_registry/results.parquet` (append-only,
  currently 13 distinct `input_name`s / 3,893 rows).
- `evaluation/trades.py::simulate(rule, cache, notional)` is a generic
  next-close trade-simulation engine over a `TradeRule` (entries/exits as
  callables) and a `dict[symbol -> DataFrame]` cache — this is the exact
  function the live app re-invokes on every parameter change, no new
  backtest logic required.
- `evaluation/adapters.py::rating_cache()` builds that cache for TV-rating-
  style signals (the only signals with a threshold-cross `TradeRule` today).
- `generate_eval_report.py::find_latest(name)` / `load_run(run_dir)` locate
  and load a run's artifacts (`results.json`, `panel.parquet`,
  `trades.parquet`) from `storage/reports/eval/<name>_<ts>/` — reused
  as-is, not reimplemented.
- The earlier one-off `tv_rating_eval.py` / `generate_tv_rating_report.py`
  (2026-07-17/18, Fable-planned) had more visual interactivity than the
  current unified report (a 69-symbol dropdown price+trades chart,
  cumulative P&L with per-trade annotations) — its statistics were folded
  into the unified framework, but that visual interactivity was not carried
  forward. This app restores and extends it, framework-wide rather than
  TV-rating-specific.

## Architecture & data flow

New root-level file `backtest_app.py`, sibling to `evaluate.py` and
`generate_eval_report.py`. Not wired into `run_all.py`/`curated.py`/pipeline-
catalog tests — an analysis tool over already-curated/already-evaluated data,
same boundary the rest of the eval framework already follows.
`generate_eval_report.py` is untouched and keeps producing the portable static
HTML; this app is the day-to-day live exploration tool, not a replacement.

```
storage/eval_registry/results.parquet ─┐
                                        ├─> Signal dropdown, populated from
storage/reports/eval/<name>_<ts>/      │    registry.load()["input_name"].unique()
  results.json / panel.parquet /  ─────┘
  trades.parquet
        │
        │ find_latest(name) + load_run(run_dir)   [reused from
        ▼                                           generate_eval_report.py]
  per-signal artifacts loaded into memory once per selection

evaluation.adapters.rating_cache() ──> dict[symbol -> DataFrame]
  (only for signals with a TradeRule; built lazily on first selection,
   kept in a module-level server-side cache keyed by signal name — NOT
   round-tripped through dcc.Store, which would serialize the full
   69-symbol/36-year panel to browser-side JSON on every interaction)

Slider change -> build TradeRule(entries=lambda df: ..., exits=lambda df: ...)
              -> evaluation.trades.simulate(rule, cache)   [in-process,
                 no disk I/O — cost bounded by in-memory panel size]
              -> callback updates trade summary, symbol chart, P&L chart
```

**Framework:** Dash (Plotly), chosen over Streamlit specifically for native
callback-graph support of cross-filtering (click one chart, filter another).
Altair was considered for its declarative selection/linked-brushing API, but
deferred for v1 — no view currently needs it enough to justify the embedding
friction (Altair charts aren't native to Dash's `dcc.Graph`, which is
Plotly-only). Documented here as an explicit option to revisit during
implementation if a concrete Altair-shaped need emerges (e.g. faceted
small-multiples across symbols).

## Views & interactions

```
+-----------------------------------------------------------------------+
| Signal: [ tv_rating (rating_all) v ]   Run: a3f9c1e2 . 1990-2026-07    |
|                                         loaded 14:32:07  [ Refresh ]   |
+---------------------------------+-------------------------------------+
| IC & Significance                | Live Trade Rule                    |
|  IC-by-horizon (Plotly bar)      |  Bull entry:  --o------  0.50      |
|  Spread + CI (Plotly)            |  Exit long :  --o------  0.10      |
|  (reused from generate_          |  Bear entry:  ------o--  -0.50     |
|   eval_report.py's chart-        |  Exit short:  ------o--  -0.10     |
|   builder functions, not         |  -> n=1,847 trades | win 41.2%     |
|   duplicated)                    |     $612,340 net  (baseline:       |
|                                   |     21,938 / 36.6% / $378,073)     |
+---------------------------------+-------------------------------------+
| Symbol Explorer:  [ AAPL v ]                                           |
|  price line + up-arrow long entry / down-arrow short entry / x exit    |
|  markers, colored by realized win/loss, recomputed from current sliders|
+-------------------------------------------------------------------------+
| Cumulative P&L (all symbols, current threshold settings)                |
|  running sum by exit date, per-trade $/% annotations on hover           |
+-------------------------------------------------------------------------+
```

**Interaction rules:**
- **Signal dropdown** → reload that signal's artifacts + rebuild the cache
  (spinner shown); sliders reset to that signal's recorded defaults
  (`BULL_MIN`/`EXIT_LONG_MAX`/etc.). Signals with no `TradeRule` (most
  factor-panel/sentiment signals today) show a disabled empty-state for the
  Live Trade Rule / Symbol Explorer / Cumulative P&L panels — only IC &
  Significance is active for those.
- **Slider changes** → re-run `evaluation.trades.simulate()` against the
  cached in-memory panel only. IC & Significance does **not** recompute (IC
  measures the raw signal vs. returns, independent of any trade rule) — only
  the Live Trade Rule summary, Symbol Explorer markers, and Cumulative P&L
  update. Sliders use Dash's `updatemode='mouseup'` so simulation runs once
  per adjustment, not once per pixel of drag.
- **Symbol dropdown** → filters the current (slider-dependent) trade set to
  that symbol; the price line always renders regardless of whether that
  symbol had any trades.
- **Refresh button** → re-runs `find_latest()` for the current signal; if a
  newer run exists, reloads and shows the new `run_id`/timestamp.
- Baseline comparison (recorded registry win-rate/$-total vs. live slider
  result) shown inline for at-a-glance tuning feedback.

## Error handling & robustness

- **No `TradeRule`/cache for this signal** → Live Trade Rule / Symbol
  Explorer / Cumulative P&L show "no trade rule defined for this signal"
  instead of empty/broken charts. Detected once at signal-select time.
- **Zero trades at current threshold** → summary shows "0 realized trades at
  this threshold"; empty (not broken) chart panels — mirrors
  `evaluation/trades.py`'s existing empty-DataFrame return, no new engine
  logic needed.
- **Symbol with zero trades** → price line still renders, no markers.
- **Registry lists a signal whose local artifact directory is missing**
  (registry synced across machines, artifacts are local-only/gitignored) →
  dropdown entry shows a warning badge; selecting it shows "no local
  artifacts for this run — run `evaluate.py --adapter ...` first" instead of
  a stack trace.
- **Empty registry** (fresh clone, nothing evaluated yet) → app still starts;
  dropdown shows "no evaluated signals yet."
- **Staleness**: run banner always shows `run_id` + `date_range` + a
  client-side "loaded at HH:MM:SS" timestamp. No background polling —
  **Refresh** is explicit and manual, so you always know whether you're
  looking at an old run or one you just kicked off elsewhere.
- **Performance**: per-symbol cache kept in a module-level server-side dict
  keyed by signal name (not `dcc.Store`, which would serialize the full
  panel to browser JSON on every interaction).

## Testing

- `evaluation/trades.py` and the rest of the evaluation engine already have
  test coverage — untouched, no new tests there.
- New tests for `backtest_app.py`'s own logic, written as **plain functions
  the Dash callbacks wrap** (not a browser/Selenium harness): rule-building
  from slider values, empty-state detection, the baseline-vs-live summary
  diff, and the artifacts-missing detection.
- `generate_eval_report.py`'s `find_latest`/`load_run`/chart-builder
  functions currently have **no tests** at all, and `backtest_app.py` will
  depend on them directly — add a small test file covering just the
  functions this app reuses (not a full retrofit of that script).

## Out of scope (this round)

- Altair — deferred; documented above as an explicit option to revisit
  in-implementation, not ruled out.
- Retiring `generate_eval_report.py` — both reporting paths stay.
- Multi-signal comparison views / a registry-browser landing page — v1 is
  deep-dive-one-signal-at-a-time; breadth (more signals in the dropdown)
  doesn't require new UI.
- Wiring into `run_all.py` or any daily-refresh automation.
- Any live/paper trading execution — exploration and parameter tuning only.
