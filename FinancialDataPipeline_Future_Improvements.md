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

### Schwab Real-Time Quotes + Options with Greeks + News Sentiment
**Status:** Implemented 2026-06-19

**`schwab_quotes_pipeline.py`** (Schwab `/quotes` batch endpoint):
- Single batched call for DJI components + sector ETFs (up to 500 symbols per request)
- Fields: last/open/high/low/close, bid/ask, 52-week range, PE, EPS, dividend yield/amount/dates
- Output: `storage/raw/schwab/quotes/quotes_{YYYYMMDD}.parquet` | CATALOG: `schwab_quotes`

**`schwab_options_pipeline.py`** (Schwab `/chains` endpoint):
- Full options chain with greeks: delta, gamma, theta, vega, rho
- Configurable symbols (`--symbols`) and weeks out (`--expirations`)
- Default: top 10 liquid equities + indexes, 4 weeks out, 40 strikes per expiration
- Output: `storage/raw/schwab/options/schwab_options_incremental_{YYYYMMDD}.parquet` | CATALOG: `schwab_options`

**`news_sentiment_pipeline.py`** (Claude `claude-haiku-4-5`):
- Scores existing Finnhub news headlines + summaries — only articles not yet scored are processed
- Batches 20 articles per Claude API call for cost efficiency
- Fields: sentiment (bullish/bearish/neutral), score (-1.0 to +1.0), confidence, key_topics
- Output: `storage/raw/finnhub/news_sentiment/news_sentiment_{mode}_{YYYYMMDD}.parquet` | CATALOG: `news_sentiment`
- Requires: `pip install anthropic` + `ANTHROPIC_API_KEY` in `.env`

**`analytics/events.py`** — new sentiment functions:
- `news_sentiment(symbols, days=7)` — recent scored headlines sorted by date
- `sentiment_summary(symbols, days=7)` — aggregate bullish/bearish/neutral counts + avg_score per symbol

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

---

### Data Validation Layer
**Status:** Implemented 2026-06-20

**`validate.py`** — standalone validation module:
- `validate_df(table, df, check_freshness=True)` — validate a fresh DataFrame right before writing; call inside any pipeline
- `validate_table(table)` — load latest snapshot from disk and validate it
- `validate_all()` — run on all CATALOG tables, return summary DataFrame (table | status | errors | warnings | rows | latest_file)

**Check categories:**
- `not_empty` — 0-row output → ERROR
- `required_cols` — any required column absent → ERROR
- `nulls:<col>` — critical column >50% null → ERROR; 5–50% null → WARNING
- `future_dates` — date column has values > today → WARNING
- `row_count` — new DataFrame < 50% the size of prior snapshot → WARNING (catches silent API failures)
- `range:<col>` — value outside expected bounds (e.g. sentiment score outside [-1, 1]) → WARNING
- `fetched_at` — newest timestamp older than 2h (when called inline) → WARNING

**CLI:**
```bash
python validate.py                  # health check — all tables with data
python validate.py --table prices   # single table detail
python validate.py --all            # include tables with no data yet
```

**`tests/test_validation.py`** — 20 tests covering schema completeness, all check severities, and validate_all behavior.

**Total test suite: 93 passed, 12 skipped.**

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

### 3. ~~Partition-Aware Storage Layout~~ ✓ COMPLETED
**Status:** Implemented 2026-06-22

All 16 pipelines now write to Hive-style partitioned directories:

```
storage/raw/prices/year=2026/month=06/prices_incremental_20260622.parquet
storage/raw/fundamentals/annual/year=2026/month=06/fundamentals_full_annual_20260622.parquet
```

**`storage_utils.py`** — shared write helper:
- `write_partitioned(df, output_dir, filename)` — derives `year`/`month` from `fetched_at` column, creates `year=YYYY/month=MM/` subdirs, writes Snappy-compressed Parquet
- `find_parquet_files(directory)` — recursive `**/*.parquet` glob for any directory

**`query.py` CATALOG** — all 28 glob patterns updated to `dir/**/*.parquet`; DuckDB views use `hive_partitioning=True` so `year` and `month` are virtual columns queryable in SQL.

Six tables that shared parent directories were split into subdirectories to avoid glob overlap:
- `options/metrics/` and `options/chain/` (was `options/`)
- `fundamentals/annual/` and `fundamentals/quarterly/` (was `fundamentals/`)
- `gas_prices/spot/` and `gas_prices/retail/` (was `gas_prices/`)

**`validate.py`** and **`tests/test_catalog.py`** updated for recursive globs.

**Total test suite: 111 passed, 12 skipped.**

---

### 4. ~~Data Validation Layer~~ ✓ COMPLETED

---

### 5. ~~Unified Pipeline Runner~~ ✓ COMPLETED
**Status:** Implemented 2026-06-20

**`run_all.py`** — 15-pipeline staged runner:
- **Stage 1** (free/public): commodity_macro, gas_prices, futures, short_interest, finnhub, finnhub_events, dividends, fundamentals
- **Stage 2** (Schwab): prices, sector_etfs, schwab_quotes, schwab_options, options_chain
- **Stage 3** (derived): synthetic_options (needs prices), news_sentiment (needs finnhub_news)

Gracefully skips pipelines with missing env vars — Schwab pipelines auto-skip when SCHWAB_* are absent; news_sentiment skips without ANTHROPIC_API_KEY. Post-run validation via `validate_table()` after each successful pipeline (disable with `--no-validate`).

**CLI:**
```bash
python run_all.py                        # incremental run (all stages)
python run_all.py --backfill             # full available history
python run_all.py --stage 1              # free/public sources only
python run_all.py --only commodity_macro,finnhub
python run_all.py --skip fundamentals,synthetic_options
python run_all.py --dry-run              # print commands, don't execute
```

**`tests/test_runner.py`** — 18 tests covering registry integrity, env-var skip logic, CLI arg filtering, and dry-run behavior.

**Total test suite: 111 passed, 12 skipped.**
