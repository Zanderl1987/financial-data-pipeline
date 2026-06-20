# Financial Data Pipeline — Future Improvements

## Completed

### DuckDB Query Layer
**Status:** Implemented 2026-06-19

A comprehensive `query.py` module plus a full `analytics/` subpackage, both placed on the D drive master store and synced to the working clone.

**`query.py`** — low-level DuckDB interface:
- `CATALOG` — 15-table glob registry (prices, options, fundamentals, macro, gas, futures, COT, earnings, insider transactions)
- `load(table, symbol, series_id, metric, start, end, columns, limit)` — push-down filtered loads
- `sql(query)` — raw SQL against all registered views
- `tables()`, `schema()`, `symbols()`, `date_range()` — discovery helpers
- `reload()` — re-registers views after a pipeline run

**`analytics/`** — high-level domain functions:
- `fundamentals.py` — `yoy_growth`, `valuation` (P/E, P/S, P/B), `top_by_metric`
- `events.py` — `upcoming_earnings`, `insider_sentiment`, `earnings_surprise`
- `options.py` — `iv_summary`, `put_call_ratio`
- `macro.py` — `rate_environment` (yield curve wide format), `inversion` (2s10s spread), `commodity_vs_symbol`

---

### Dividend Pipeline + Sector ETF Pipeline
**Status:** Implemented 2026-06-19

**`dividend_pipeline.py`** (Finnhub `/stock/dividend2`):
- Per-symbol cash dividend history: ex-date, pay-date, record-date, declaration-date, amount, adj_amount, frequency, currency
- `--backfill` fetches 10 years; incremental default is 2 years
- Output: `storage/raw/finnhub/dividends/dividends_{mode}_{YYYYMMDD}.parquet`
- CATALOG key: `dividends`

**`sector_etf_pipeline.py`** (Schwab API):
- Daily OHLCV for 11 SPDR sector ETFs (XLK/XLF/XLE/XLV/XLY/XLI/XLC/XLRE/XLP/XLU/XLB) + 4 broad indexes (SPY/QQQ/IWM/DIA)
- Same schema as prices table (OHLCV + pct_change/log_return/intraday_range/vwap + sector label)
- Output: `storage/raw/sector_etfs/sector_etfs_{mode}_{YYYYMMDD}.parquet`
- CATALOG key: `sector_etfs`

**`analytics/events.py`** — new dividend functions:
- `dividend_history(symbols, start)` — full dividend history, ex-date sorted
- `dividend_calendar(days_ahead=60)` — upcoming ex-dates in window

**`analytics/sectors.py`** — new module:
- `sector_performance(start, end)` — total return % per ETF over period
- `sector_vs_spy(start)` — each sector's return relative to SPY
- `sector_rotation(lookback_days=20)` — momentum ranking by avg log return

---

### CATALOG Expansion + Credit Spreads
**Status:** Implemented 2026-06-19

**CATALOG fix (`query.py`):** Registered the 7 Finnhub tables that `finnhub_pipeline.py` already fetches but were invisible to the query layer: `finnhub_profile`, `finnhub_quotes`, `finnhub_metrics`, `finnhub_recommendations`, `finnhub_price_targets`, `finnhub_upgrades`, `finnhub_news`. They show "no data" until the pipeline runs; after that they're queryable like any other table.

**Credit spreads (`commodity_macro_pipeline.py`):** Added 4 ICE BofA OAS series to the FRED SERIES catalog:
- `BAMLH0A0HYM2` — HY Credit Spread (OAS)
- `BAMLC0A0CM` — IG Corporate Spread (OAS)
- `BAMLH0A0HYM2EY` — HY Effective Yield
- `BAMLEMCBPIOAS` — EM Corporate Spread (OAS)

Note: VIX (`VIXCLS`) was already present in the macro pipeline.

**`analytics/macro.credit_spreads()`:** New function — loads all credit spread series and pivots to wide format (`date | hy_spread | ig_spread | hy_yield | em_spread`). Includes interpretation thresholds (hy_spread > 500 bps = stress).

---

### Snappy Compression on All Parquet Outputs
**Status:** Implemented 2026-06-19

All `to_parquet()` calls across every pipeline now pass `compression="snappy"`. This reduces file sizes by roughly 40–60% compared to uncompressed Parquet, with faster read/write speeds than heavier codecs like gzip or brotli. Snappy trades a slightly larger file size for significantly faster decompression — the right default for a pipeline that reads its own output frequently.

Files updated:
- `commodity_macro_pipeline.py`
- `fundamentals_pipeline.py`
- `futures_pipeline.py`
- `gas_price_pipeline.py`
- `options_chain_pipeline.py`
- `price_history_pipeline.py`
- `synthetic_options_pipeline.py`
- `yahoo_options_pipeline.py`

---

## Candidate Improvements

### 1. ~~DuckDB Query Layer~~ ✓ COMPLETED
**Priority: High | Effort: Low** — see Completed section above

DuckDB can query Parquet files directly with SQL — no database server, no ETL, no catalog. It reads multiple files in one query using glob patterns.

```python
import duckdb
df = duckdb.query("""
    SELECT symbol, date, close
    FROM 'storage/raw/prices/prices_*.parquet'
    WHERE symbol = 'AAPL'
    ORDER BY date
""").df()
```

This would be the single highest-value addition to this pipeline. Useful for:
- Cross-pipeline joins (e.g. options chains joined to fundamentals)
- Backtest data slicing without loading entire files into memory
- Ad hoc analysis without writing pandas boilerplate

**Recommendation:** Add a `query.py` helper or a `notebooks/` directory with example DuckDB queries against the stored Parquet files.

---

### 2. Apache Iceberg Tables
**Priority: Low–Medium | Effort: High**

Apache Iceberg is an open table format that sits on top of Parquet files and adds:
- **Time travel** — query data exactly as it existed at any past snapshot
- **Schema evolution** — add/rename columns without rewriting old files
- **ACID transactions** — safe concurrent writes from multiple processes
- **Partition pruning** — the catalog knows which files contain which date ranges, so queries skip irrelevant files automatically

**Why not yet:** Iceberg requires a catalog backend (SQLite, REST server, AWS Glue, or Hive Metastore) and meaningful operational overhead. The dated filename convention already provides a simpler form of time-travel (`prices_incremental_20260619.parquet`), which is sufficient at current scale.

**When to revisit:** If total stored data exceeds ~50GB, if multiple processes need to write concurrently, or if time-travel queries across all history become a regular need. At that point, [PyIceberg](https://py.iceberg.apache.org/) with a local SQLite catalog is the lowest-friction entry point.

---

### 3. Partition-Aware Storage Layout
**Priority: Medium | Effort: Medium**

Currently files are stored flat with the date encoded in the filename. A Hive-style partition layout would make range queries faster when using DuckDB or Spark:

```
storage/raw/prices/year=2025/month=06/prices.parquet
storage/raw/prices/year=2026/month=06/prices.parquet
```

DuckDB and PyArrow both understand Hive partitioning natively via `dataset.partitioning("hive")`. This is a stepping stone toward Iceberg without the catalog overhead.

---

### 4. Data Validation Layer
**Priority: Medium | Effort: Low–Medium**

Add lightweight schema + freshness checks after each pipeline run:
- Row count plausibility (warn if output is >50% smaller than previous run)
- Expected columns present and correctly typed
- No future dates in the `date` column
- `fetched_at` within the last hour

[Pandera](https://pandera.readthedocs.io/) or a simple hand-rolled check function would work. Catches silent data quality regressions early.

---

### 5. Unified Pipeline Runner
**Priority: Low | Effort: Medium**

A `run_all.py` script (or a `Makefile`) that runs all pipelines in dependency order and logs success/failure per pipeline. Would make daily incremental runs a single command instead of invoking each file individually.
