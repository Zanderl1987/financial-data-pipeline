# Architecture

## Layers

```
*_pipeline.py (repo root, one per source)
  └─ storage_utils.write_partitioned()
       → storage/raw/<category>/year=YYYY/month=MM/*.parquet
            └─ curated.py dedup
                 → storage/curated/<table>/<table>.parquet
                      └─ query.py  (DuckDB CATALOG; q.load()/q.sql())
                           └─ analytics/
                                (features.py PIT panel, signals.py factors,
                                 technical.py TA + tv_rating, + per-domain
                                 modules: fundamentals, macro, sectors,
                                 short_interest, options, events, exposure)
                                └─ backtest.py (quantile portfolios)
                                   event_backtest.py (event studies)
                                     └─ signal_monitor.py (signal-health tracking)
```

### 1. Ingestion — `*_pipeline.py`

One standalone script per data source at the repo root (~85 of them). Each:

- Fetches from a free/public API or scrapes a public page.
- Normalizes to lowercase snake_case columns plus a `fetched_at` ISO
  timestamp.
- Writes via `storage_utils.write_partitioned()`, which Hive-partitions on
  `year=YYYY/month=MM` derived from `fetched_at` (or current UTC time if
  absent).
- Exposes an argparse CLI, typically with `--backfill` for full history vs.
  a default incremental window, plus a rate limiter and 429 retry/backoff.

Every run of a pipeline writes a **new** dated file — it never edits or
deletes prior output. That's deliberate (append-only, crash-safe) but means
the raw store accumulates overlapping windows from incremental re-fetches.

### 2. Raw storage — `storage/raw/`

Hive-partitioned Parquet, one subtree per source/category (e.g.
`storage/raw/eia/petroleum_stocks/year=2026/month=07/*.parquet`). This is the
ground truth — nothing here is ever mutated, only appended to. **Never query
it directly**: overlapping incremental windows have measured up to ~42%
duplicate rows on some tables (`fundamentals_annual` was the worst offender
found so far).

### 3. Dedup — `curated.py`

Collapses each raw table down to one row per natural key (most-recently
fetched version wins) and writes a single compacted file to
`storage/curated/<table>/<table>.parquet`. The natural key per table lives in
`curated.py`'s `KEYS` mapping — this is what "wire a new pipeline" step 4
means by "curated.py KEYS (natural dedup key)".

`run_all.py` calls this automatically after every run. If you invoke a
pipeline script directly, run `curated.py` yourself afterward or every
downstream layer reads stale/duplicated data.

### 4. Query layer — `query.py`

A `CATALOG: dict[str, str]` maps ~130 logical table names to glob patterns.
`query.reload()`/`_register_views()` registers a DuckDB view per table that
reads the curated file when one exists (`USE_CURATED = True`, the default)
and falls back to the raw glob otherwise.

Some pipelines write multiple sibling tables into one storage directory
(e.g. `treasury_tic`, `google_trends`, `reddit`) — for those, the glob must
match on filename prefix (`dir/**/name_*.parquet`) rather than a bare
`**/*.parquet`, or DuckDB's `union_by_name` will merge mismatched schemas
across tables. `tests/test_catalog.py` guards against this collision.

```python
import query as q
df = q.load("prices", symbol="NVDA", start="2025-01-01")
q.tables()        # every table + row count
q.schema("prices")
q.date_range()     # min/max dates, all tables or one
q.reload()         # refresh views after a pipeline run
```

### 5. Analytics — `analytics/`

Per-domain modules (`fundamentals.py`, `macro.py`, `sectors.py`,
`short_interest.py`, `options.py`, `events.py`, `exposure.py`,
`event_impact.py`, `relevance.py`) plus two load-bearing ones:

- **`features.py`** — builds the point-in-time feature matrix. See
  [Point-in-time correctness](#point-in-time-correctness) below; this is the
  single most important invariant in the repo.
- **`signals.py`** — turns the feature matrix into 9 z-scored cross-sectional
  factors (`signal_panel()`): momentum, value, quality, low-volatility,
  short-pressure, insider-flow, sentiment. `rank_symbols()` composites them.
- **`technical.py`** — TA indicators and the TradingView-rating replication
  used by `signal_monitor.py` and `event_backtest.technical_events()`.

### 6. Backtesting

Two complementary engines, not a hierarchy — pick based on the question:

- **`backtest.py`** — cross-sectional: takes a `(symbol, date, score)` panel
  from `signals.py`, ranks into quantile portfolios, rebalances on a
  schedule, reports an equity curve and risk/return metrics vs. equal-weight
  buy-and-hold. Vectorized: portfolio returns are a single weight-matrix ×
  return-matrix product, no per-day Python loop.
- **`event_backtest.py`** — conditional: takes any `(date)` or
  `(symbol, date)` event stream and measures the price path before/after,
  with abnormal returns vs. a benchmark, cross-event t-stats, and an
  unconditional base rate. Built-in event generators: `earnings_events()`,
  `filing_events()`, `drawdown_events()`, `price_move_events()`,
  `threshold_events()`, `technical_events()`. A scenario tester turns events
  into a trade list (entry lag, holding period, stop/take-profit) with win
  rates and an equity curve.

Both share one invariant: **look-ahead safety**. A score/signal known as of
date *t* can only earn the return of *t+1* onward — weights are always
shifted one day before being multiplied into returns. Never remove that lag
to "fix" a result that looks too weak.

`event_backtest.load_close()` deliberately keeps the *longest* available
price series across price tables so a shallow watchlist-only pull doesn't
shadow a deeper history table for the same symbol — preserve that when
touching price-loading code.

### 7. Signal health — `signal_monitor.py`

A maintained backtest: re-scores configured `(signal, symbols)` pairs over
several trailing windows using `event_backtest.technical_events()` +
`scenario()` (no new backtest math — it reuses the event engine), appending
one row per `(signal, window)` run to a history file so accuracy drift is
visible over time. Trailing windows restrict which *event dates* are
considered, not how much price history is loaded, so indicator warm-up
(e.g. a 200-day SMA) is never truncated by picking a short window.

## Point-in-time correctness

`analytics/features.py` ASOF-joins every feature onto the date it was
**knowable** (filing/publication date + an explicit lag), never the date it
was merely observed or the date the pipeline happened to fetch it. This is
the property that makes every backtest in this repo meaningful rather than
optimistic — a look-ahead bug here is silent (nothing errors) and corrupts
every downstream quantile spread and event-study result.

Any new feature block added to `features.py` must join on a knowable-as-of
date. When in doubt, check how an existing similarly-lagged feature in that
file does it and match the pattern rather than introducing a new join style.

One known exception, and it's deliberate: the earnings-transcript dataset in
the sibling `custom_index_tool` repo uses `entry_lag=0` because that pull is
building a **label** (was this quarter's news good or bad, in hindsight), not
a tradeable signal — don't "fix" it to match this repo's signal-lag
convention.

## Storage layout reference

```
storage/
  raw/<category>/[<subcategory>/]year=YYYY/month=MM/*.parquet   # append-only, never mutated
  curated/<table>/<table>.parquet                                # one deduped file per CATALOG table
  iceberg/constituents_catalog.db + constituents/                # Apache Iceberg tables (index members,
                                                                    securities, fund holdings, identifier map)
  quality_reports/                                                # weekly validate.py output archive
  tmp/                                                            # scratch (e.g. options contract lists mid-run)
  backup/
```

## Dependency stages (`run_all.py`)

1. **Stage 1** — free/public sources (FRED, EIA, yfinance, Finnhub, SEC
   EDGAR, CFTC, …). No auth beyond a free API key.
2. **Stage 2** — Schwab-authenticated (prices, sector ETFs, real-time
   quotes, options chains). OAuth is interactive, so these never run
   unattended in an automated/scheduled context.
3. **Stage 3** — derived pipelines that depend on earlier stages'  output
   (e.g. `synthetic_options_pipeline.py` needs Stage 2 prices;
   `news_sentiment_pipeline.py` scores Stage 1 news).

`run_all.py --dry-run` prints the exact command for every registered
pipeline without executing anything — the fastest way to see current wiring.
