# Financial Data Pipeline — Future Improvements

## Completed

### Value-Extraction Stack: Curated Layer + Feature Matrix + Signals + Backtest
**Status:** Implemented 2026-06-29

The store had grown to ~85 raw tables but only ~21 analytics functions — heavy
on ingestion, light on extraction. This session built the four layers that turn
the breadth into usable, trustworthy signal. Full suite: **173 passed, 13 skipped.**

**`curated.py`** — deduplicated/compacted snapshots (fixes a silent correctness bug):
- The query layer globs *every* dated incremental file with `union_by_name=True`
  and no dedup. Because incremental pipelines re-fetch overlapping windows, the
  same logical row was written many times. Measured: **4.46M duplicate rows —
  42.4% of the entire store** (`institutional_holdings` 84%, `gas_retail` raw
  97%, `fundamentals_annual` 51%). Every COUNT/AVG/return in analytics/ was
  computing over duplicates.
- `compact(table)` / `compact_all()` dedup each table on a natural key (keeping
  the latest `fetched_at`), writing one Snappy file to
  `storage/curated/<table>/<table>.parquet`.
- `KEYS` registry of per-table natural keys; tables without a key (or whose key
  columns aren't all present) fall back to **safe full-row dedup** — a partial
  key would silently merge distinct rows, so it's never used.
- Always sources from raw (`_raw_reads` context) so re-runs rebuild cleanly.
- `query.py` now **prefers the curated snapshot** when one exists (toggle
  `q.USE_CURATED`), so the whole analytics stack reads clean data with no API
  change. CLI: `python curated.py [--table T | --summary | --check]`.

**`analytics/features.py`** — `feature_matrix()` point-in-time (symbol, date) panel:
- Price features (returns 1/21/63/252d, 12-1 momentum, 21d realized vol, dollar
  volume), point-in-time fundamentals via **DuckDB ASOF JOIN on the SEC `filed`
  date** (no look-ahead), and broadcast macro series (DGS10/DGS2/VIX/2s10s/HY OAS).
- Every block is guarded — missing source tables are skipped, panel still builds.
  Price source auto-detects (`prices` → `tiingo_prices` → `sector_etfs`).

**`analytics/signals.py`** — cross-sectional factor library:
- `momentum`, `value` (earnings yield), `quality` (ROA + gross margin),
  `low_vol`, `growth` (PIT YoY revenue). Each z-scored within each date; the
  `composite` is a weight-renormalized blend of whichever factors have data.
- `signal_panel()` (full panel), `rank_symbols()` (latest-date ranking with a
  `rank` column), plus single-factor wrappers. Custom `weights=` tilt the blend.

**`backtest.py`** — vectorized signal→returns evaluator:
- Quantile portfolios (long top / short bottom), D/W/M/Q rebalance, transaction
  costs in bps, equal-weight buy-and-hold benchmark.
- **Look-ahead safe:** weights set on rebalance date *t* are lagged one day, so a
  score earns the return of *t+1* onward. Verified by a perfect-foresight test
  (Sharpe +44) mirrored exactly by its negation (−44).
- `BacktestResult` carries `.equity`, `.returns`, `.weights`, `.metrics`
  (CAGR, ann vol, Sharpe, max drawdown, hit rate, turnover, vs-benchmark) and
  `.summary()`.

**Tests:** `tests/test_curated.py`, `test_features.py`, `test_signals.py`,
`test_backtest.py` — 34 new tests (dedup keying, PIT feature math, z-score/
composite renormalization, portfolio construction + look-ahead safety).

**Post-run compaction wired into `run_all.py` (2026-06-29):** after every run,
`compact_curated()` rebuilds curated snapshots for exactly the tables whose
pipeline PASSed (union of their `tables`), keeping `storage/curated/` in sync
with new raw files. Skipped on `--dry-run`; opt out with `--no-compact`. Errors
are swallowed so compaction can never sink a run. Header now shows `Compact:`.
Covered by 4 new tests in `tests/test_runner.py` (suite: 177 passed, 13 skipped).

**Follow-ups not yet done:** add short-interest/insider/sentiment signal blocks
(tables were empty in this clone); split the catalog glob collisions
(`treasury_tic_holders`/`_slt`, `google_trends_*`, `reddit_*`) once discriminator
columns are known (`FILTERS` hook is in place in curated.py).

---

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

### 8. ~~USDA NASS + US Census Trade Pipelines~~ ✓ COMPLETED
**Status:** Implemented 2026-06-23

**`usda_pipeline.py`** (USDA NASS QuickStats API — requires `USDA_NASS_API_KEY`):
- 8 major US field crops: corn, soybeans, wheat, cotton, rice, sorghum, barley, oats
- Annual national production statistics: area planted/harvested, yield, production, price received
- Fertilizer prices paid by farmers: anhydrous ammonia, DAP, urea, potash (monthly, national)
- Backfill from 2000; incremental = last 5 years
- CATALOG: `usda_crops`, `usda_fertilizers`; Stage 1 in `run_all.py`

**`trade_pipeline.py`** (US Census Bureau International Trade API — requires `CENSUS_API_KEY`):
- 5 agricultural HTS chapters: cereals (10), oilseeds (12), fats/oils (15), feed residues (23), fertilizers (31)
- World totals only (`CTY_CODE=0000`) — imports and exports separately
- Backfill = annual YTD totals (December) from 2010; incremental = last 24 months monthly
- CATALOG: `us_imports_hs`, `us_exports_hs`; Stage 1 in `run_all.py`

**Total test suite: 111 passed, 12 skipped.** CATALOG expanded 51→55 tables.

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

### 6. ~~4 New Commodity Pipelines~~ ✓ COMPLETED
**Status:** Implemented 2026-06-23

Four new free-data-source pipelines, no new API keys required. CATALOG expanded from 45 to 50 tables. All 111 tests still passing.

**`imf_commodities_pipeline.py`** (FRED API — IMF PCPS mirror):
- 14 confirmed-working series: base metals (Al/Ni/Zn/Pb/Fe/Sn), coal, LNG (Japan + European), rice, palm oil, tea, Non-Fuel Commodity Index, Food Commodity Index
- Supplements `commodity_macro_pipeline.py` — no overlap with existing series
- CATALOG: `imf_commodities`

**`metals_pipeline.py`** (FRED API + api.metals.live):
- 7 base metals via FRED IMF PCPS monthly series: Cu, Al, Ni, Zn, Pb, Fe (iron ore), Sn
- api.metals.live real-time spot (SSL error on current host — graceful fallback)
- Precious metals (Au, Ag, Pt, Pd) already in `commodity_macro_pipeline.py`
- CATALOG: `metals_spot`

**`fao_pipeline.py`** (FAOSTAT bulk ZIP fallback):
- Crop production (quantities + harvested area) for 10 major countries, 17+ commodities
- Producer prices (USD/tonne) for 12 key commodities
- Primary method: FAOSTAT REST API (fenixservices.fao.org) — returns HTTP 521 frequently
- Fallback: bulk ZIP at `https://bulks-faostat.fao.org/production/` (reliable; ~25MB + ~8MB)
- CATALOG: `fao_production`, `fao_prices`

**`worldbank_pink_sheet.py`** (World Bank Pink Sheet Excel):
- 71 commodities, monthly prices 1960–present (~50,000 rows)
- Covers fertilizers (urea, DAP, potash, phosphate), rubber, cocoa, and other series absent from FRED
- URL hash changes monthly — update `PINK_SHEET_URLS[0]` each month (find at worldbank.org/en/research/commodity-markets)
- Excel structure: rows 0-3 = title, row 4 = commodity names, row 5 = units, row 6+ = "1960M01" dates
- Required openpyxl >= 3.1.5 (upgraded from 3.0.10 during implementation)
- CATALOG: `wb_commodities`

**`bls_pipeline.py`** — added 7 supply-chain input cost PPI series:
- Plastics/Resin Mfg (PCU325211325211), Nitrogenous Fertilizer Mfg (PCU325311325311A), Synthetic Ammonia/Urea (WPU0652013A), Industrial Chemicals (WPU061), Metals/Metal Products (WPU10), Computer/Electronic Product Mfg (PCU3334), Softwood Lumber (WPU0571)

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

---

### 7. ~~NOAA NCEI Climate Pipeline~~ ✓ COMPLETED
**Status:** Implemented 2026-06-23

**`noaa_climate_pipeline.py`** (NOAA NCEI Access Services API v1 — keyless, no token required):
- 15 stations covering major US agricultural regions: Corn Belt (Des Moines), Winter Wheat (Wichita), Central Valley (Fresno), Spring Wheat (Minneapolis), Southeast (Atlanta), Cotton/Citrus (Phoenix), Midwest Hub (Chicago), Cotton/Cattle (Dallas), Export Hub (New Orleans), Cotton/Soybeans (Memphis), Northeast (New York), Pacific Coast (LA), Northern Plains (Great Falls), Corn/Soybeans (Omaha), Delta Ag (Jackson)
- Monthly measures: TMAX, TMIN, TAVG, PRCP, SNOW, HDD, CDD, DP01, DP10, EMXT, EMNT
- Backfill: 1990 to present; incremental: last 2 years
- CATALOG: `noaa_climate`; added to Stage 1 of `run_all.py`

**Total test suite: 111 passed, 12 skipped.**

---

### 9. ~~Finviz Pipeline~~ ✓ COMPLETED
**Status:** Implemented 2026-06-29

**`finviz_pipeline.py`** (finviz.com HTML scraping — no API key required):
- 15 datasets across 8 CATALOG tables; runs as Stage 1 in `run_all.py`
- **Market movers** (`finviz_movers`): top gainers, losers, unusual volume, new 52-week highs/lows, most volatile, overbought, oversold — all with `signal` column; up to 200 results each
- **S&P 500 overview** (`finviz_screener`): ticker, company, sector, industry, country, market cap, P/E, price, % change, volume; paginated to 500 rows
- **Financial metrics** (`finviz_financials`): ROA, ROE, ROIC, current ratio, quick ratio, LT debt/equity, gross/operating/net margins; S&P 500 universe
- **Insider trading** (`finviz_insider`): ticker, owner, role, date, transaction type, cost/share, shares, total value ($), total shares held; 10 pages (200 rows) default
- **Sector performance** (`finviz_sector_perf`): 11 GICS sectors, week/month/quarter/half/year/YTD returns
- **Industry performance** (`finviz_industry_perf`): ~140 industries, same timeframes
- **Country performance** (`finviz_country_perf`): US-listed stocks by country of origin
- **Group valuation** (`finviz_group_valuation`): P/E, Fwd P/E, PEG, P/S, P/B, P/FCF, dividend yield, analyst rec by sector and industry; `group_type` column distinguishes them

**CLI:**
```bash
python finviz_pipeline.py                             # all datasets
python finviz_pipeline.py --only movers               # just movers
python finviz_pipeline.py --only screener,financials
python finviz_pipeline.py --only insider
python finviz_pipeline.py --only groups
python finviz_pipeline.py --max-screener-results 250
python finviz_pipeline.py --insider-pages 5
```

Note: Finviz provides 15-minute delayed quotes on the free tier. Requests are throttled at 1.5s intervals.

---

### 10. ~~Stock Analysis Pipeline~~ ✓ COMPLETED
**Status:** Implemented 2026-06-29

**`stockanalysis_pipeline.py`** (stockanalysis.com HTML scraping — no API key required):
- 19 datasets across 11 CATALOG tables; runs as Stage 1 in `run_all.py`
- **Market movers** (`sa_movers`): top 100 gainers + losers per timeframe (1D/1W/1M/YTD/1Y/3Y/5Y) plus premarket gainers/losers — `signal` column distinguishes all 16 variants
- **IPO history** (`sa_ipos`): recent ~200 IPOs — date, symbol, company, IPO price, current price, return
- **IPO calendar** (`sa_ipo_calendar`): upcoming IPOs — exchange, price range, shares offered, deal size, estimated market cap, revenue
- **IPO statistics** (`sa_ipo_stats`): annual + monthly IPO counts back to 2000
- **Corporate actions** (`sa_corporate_actions`): splits, acquisitions, spinoffs, bankruptcies, symbol changes, new listings, delistings — `action_type` column; `--backfill` flag fetches all years to 1998
- **Stock reference** (`sa_stock_list`): ~500 US stocks with symbol, company, industry, market cap
- **ETF reference** (`sa_etf_list`): ETFs with symbol, fund name, asset class, AUM
- **Income statements** (`sa_income`): revenue, gross profit, EBITDA, net income, EPS, margins, FCF — wide format, annual + quarterly
- **Balance sheets** (`sa_balance`): full A/L/E including net cash, tangible book value
- **Cash flow** (`sa_cashflow`): operating/investing/financing cash flows + FCF
- **Ratios + KPIs** (`sa_ratios`): P/E, P/S, P/B, ROE, ROA, margins, debt ratios

**CLI:**
```bash
python stockanalysis_pipeline.py                              # all datasets
python stockanalysis_pipeline.py --only movers
python stockanalysis_pipeline.py --only ipos
python stockanalysis_pipeline.py --only actions
python stockanalysis_pipeline.py --only actions --backfill    # all years to 1998
python stockanalysis_pipeline.py --only reference
python stockanalysis_pipeline.py --only financials
python stockanalysis_pipeline.py --only financials --symbols AAPL,MSFT,NVDA
```

Financials default to DJI components + sector ETFs (~45 symbols). Requests throttled at 2s intervals.

---

### 11. ~~Fed Sentiment + Real Estate + Shipping Pipelines~~ ✓ COMPLETED
**Status:** Implemented 2026-07-02

Three new sources requested to fill gaps identified in a research pass over
Fed communications, real estate, and shipping/logistics (Baltic Dry Index and
Freightos were ruled out — paid/ToS-restricted for time-series use).

**`fed_sentiment_pipeline.py`** (federalreserve.gov RSS + Claude, no API key
for the fetch, `ANTHROPIC_API_KEY` for scoring):
- Pulls FOMC statements (`/feeds/press_monetary.xml`) and Fed official
  speeches (`/feeds/speeches.xml`), scrapes full text via the `#article`
  selector (present on both speech and press-release pages), scores
  hawkish/dovish stance with `claude-haiku-4-5` (same pattern as
  `news_sentiment_pipeline.py`, batches of 5 — documents are long).
- CATALOG: `fed_speeches` (raw text), `fed_sentiment` (stance/hawkish_score/confidence/key_topics)
- Caveat: RSS feeds only expose the ~15 most recent items each; no deeper
  backfill is available without FRASER archive access.

**`real_estate_pipeline.py`** (FHFA + Zillow, keyless):
- FHFA HPI master file — single CSV with all geography levels (national,
  census division, state, MSA, Puerto Rico) and both NSA/SA indexes.
- Zillow Research ZHVI (state + metro) and ZORI (metro) — wide date-column
  CSVs melted to long format. Requires a real browser User-Agent (default
  `requests` UA gets blocked).
- CATALOG: `fhfa_hpi`, `zillow_zhvi`, `zillow_zori`

**`shipping_pipeline.py`** (NY Fed GSCPI + FRED, uses existing `FRED_API_KEY`):
- GSCPI — NY Fed's composite Global Supply Chain Pressure Index, keyless
  Excel download, monthly back to 1998.
- FRED deep-sea freight transportation + marine cargo handling PPI series
  (`PCU483111483111`, `WPU301301`, `WPU3113`) — substitute for the Baltic
  Dry Index / Freightos FBX, which require paid licenses for time-series use.
- CATALOG: `shipping_gscpi`, `shipping_freight_ppi`

CATALOG expanded 78→85 tables. All 3 pipelines wired into `run_all.py` Stage 1,
`validate.py` schemas, and `curated.py` natural keys. Test suite: 189 passed,
15 skipped (schwabdev/anthropic still absent from conda env).

---

## Candidate Improvements (Next Up)

### A. Market-Wide Gainers / Losers via Yahoo Finance Screener
**Priority: High | Effort: Low**

Add `market_movers_pipeline.py` using `yfinance`'s built-in screener endpoint to get true market-wide top gainers/losers (not just S&P 500). Yahoo's screener returns ~25 top movers per call, no auth required. Add `market_movers()` function to `analytics/`.

---

### B. Portfolio Tracking Pipeline
**Priority: High | Effort: Medium**

Import actual holdings (from a CSV or Schwab `/accounts` endpoint) and overlay against prices, dividends, options exposure, and fundamentals. Would enable P&L tracking, cost basis, and position-level risk analytics.

---

### C. Scheduled Run + Freshness Dashboard
**Priority: Medium | Effort: Low**

- Windows Task Scheduler entry to run `run_all.py` on a daily schedule with a run log
- `python status.py` — one-command summary showing every CATALOG table's latest file date and row count in a clean table

---
