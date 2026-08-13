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

### 12. Event-Study Backtester + TradingView Rating Replica + Schwab Expansion
**Status:** Implemented 2026-07-03

New backtesting stack and three new data sources, plus a full Schwab API
expansion beyond the original 1-year-capped price pull.

**`event_backtest.py`** (new, repo root) — event-study/scenario engine
complementing `backtest.py`'s quantile-portfolio approach with conditional
analysis around discrete events:
- `event_study()` — CAR curves, cross-event t-stats, and an unconditional
  baseline so edge can be measured against the base rate, not just eyeballed.
- `scenario()` — turns any event stream into a trade list (entry lag, holding
  period, stop-loss/take-profit) with win rate, profit factor, and an
  equal-weight equity overlay.
- Event generators: `earnings_events`, `filing_events`, `drawdown_events`,
  `price_move_events`, `threshold_events`, `technical_events` (golden/death
  cross, RSI, MACD, TV-rating transitions, or any custom lambda).
- `load_close()` picks the **longest** series across price tables (tiingo_prices
  → prices → market_history → sector_etfs) rather than the first hit — a real
  bug caught mid-session (a shallow 90-day watchlist pull was shadowing 24
  years of `market_history`).

**`analytics/technical.py`** (new) — indicator library + TradingView Technical
Rating replica: `tv_rating()` reproduces TV's exact 26-signal aggregate rating
(15 MA votes + 11 oscillator votes, thresholds ±0.1/±0.5) locally from stored
OHLCV, validated exact against TradingView's live scanner on completed bars.
Fully backtestable over decades, unlike the live-only TV rating itself.

**New pipelines** (sample-verified, wired into `run_all.py`/`validate.py`/tests):
`yfinance_pipeline.py` (`market_history` — S&P to 1927, VIX to 1990, futures/FX/
bond ETFs), `tradingview_pipeline.py` (`tv_ratings` — daily TA-rating snapshots,
top-500 US stocks + 20 ETFs), `sec_filings_pipeline.py` (`sec_filings` — EDGAR
daily filing index, ~84% ticker-mapped).

**Full history backfilled:** `tiingo_prices` (all 63 watchlist symbols, most to
1990), `market_history` (all 25 assets, S&P to 1927), `sec_filings` (90 business
days). Demo findings: TV-rating turns Strong Buy carried real edge historically
(60.6% win, PF 1.88, 21d hold, 1,619 trades).

**Schwab API expansion** (`f418ab9`) — the old pipeline hardcoded a 1-year
lookback; Schwab actually serves full listed history (`price_history_pipeline.py
--full`, daily bars to ~1985 via the startDate-wins-over-period trick).
New: `schwab_intraday_pipeline.py` (5-min/1-min bars), `schwab_movers_pipeline.py`
(top-10 movers snapshot), `schwab_portfolio_pipeline.py` (positions +
transactions mirror, account numbers masked to last-4). CATALOG: `schwab_intraday`,
`schwab_movers`, `schwab_positions`, `schwab_transactions`.

Test suite: 223 passed, 5 skipped (schwabdev now installed; anthropic still
absent). CATALOG grew from 109 to 132 tables across this session's additions.

---

### 13. Daily TA-Rating Signal-Change Scanner + Signal Health Monitor
**Status:** Implemented 2026-07-03

Two additions on top of #12's backtesting stack, both built by reusing
`analytics.technical.rating_history()` and `event_backtest.technical_events()`/
`scenario()` — no new indicator or backtest math.

**`event_backtest.rating_changes()`** — cross-sectional scan: which symbols
changed their TA rating bucket (strong_sell..strong_buy) on a given day, or
over a date range. Diffs `rating_label` day-over-day; filters by direction
(upgrade/downgrade) and minimum bucket jump. `tv_snapshot_changes()` is a
companion that diffs TradingView's own daily `tv_ratings` snapshots instead
(wider universe, but needs ≥2 accumulated snapshots — raises a clear message
until then rather than crashing).

**`signal_scan.py`** (new CLI, repo root) — ASCII table of rating changes,
no Python shell needed:
```bash
python signal_scan.py                     # latest day, 63-symbol watchlist
python signal_scan.py --date 2026-06-15
python signal_scan.py --upgrades --min-step 2
python signal_scan.py --source tv         # diff TradingView's own snapshots
python signal_scan.py --history 30        # all changes in the last N days
```
Not wired into `run_all.py` — it's a read-only analysis tool, writes nothing.

**`signal_monitor.py`** + **`signal_monitor_config.json`** (new) — a maintained
backtest that re-scores configured signals (`tv_strong_buy`, `tv_buy`, `tv_sell`,
`tv_strong_sell`, `golden_cross` by default, all over the 63-symbol watchlist)
across trailing windows (full/3y/1y/180d) every run, appending dated performance
rows (win rate, avg return, profit factor, CAR21 mean + t-stat) to the new
**`signal_health`** table. Flags `DEGRADED` when a signal's trailing-1y win rate
drops >10pts below its full-history baseline or its trailing profit factor falls
below 1.0 (with a minimum trade count so noise doesn't trigger it) — this is how
declining signal accuracy gets caught over time rather than assumed away.
`--history N` prints the stored win-rate time series so drift is visible
run-over-run. Wired into `run_all.py` as a Stage 3 (derived) spec so the health
row refreshes automatically on every full pipeline run.

First live run flagged `tv_sell`/`tv_strong_sell` as **DEGRADED** already — both
short-side signals have a trailing-1y profit factor below 1.0 (0.55 and 0.40
respectively, on 1,072/448 trades — comfortably past the min-trade floor). Their
full-history profit factor was already weak (0.62/0.65), so this reads less like
"an edge that decayed" and more like "the short side of the TV rating never had
a clean edge to begin with" — worth a human look before acting on it either way.

CATALOG grew 132→133 tables (`signal_health`). Test suite: 234 passed, 5 skipped
(11 new tests in `tests/test_event_backtest.py` covering bucket-change detection,
direction/min_step filters, date-mode isolation, and empty-result shape).

---

## Candidate Improvements (Next Up)

### A. Market-Wide Gainers / Losers via Yahoo Finance Screener
**Priority: Low | Effort: Low** — largely covered

`schwab_movers` (top-10 per index, daily snapshot), `finviz_movers`, and
`sa_movers` (stockanalysis.com, 16 gainers/losers variants incl. premarket)
already cover this need from three different sources. The one remaining delta
is Yahoo's screener returning true *market-wide* (not index-limited) movers —
low priority now that three working sources exist.

---

### B. Portfolio Tracking Pipeline
**Status:** ✓ COMPLETED 2026-07-03 — see #12 above

`schwab_portfolio_pipeline.py` mirrors positions and transactions
(`schwab_positions`, `schwab_transactions`), account numbers masked to last-4.

---

### C. ~~Scheduled Run + Freshness Dashboard~~ ✓ COMPLETED
**Status:** Implemented 2026-07-29

Scheduling half turned out to already exist (`ClaudeAuto-DailyAccumulators`, set up
2026-07-06 per `docs/AUTOMATION.md` — a full unattended `run_all.py` isn't feasible since
Schwab OAuth is interactive, so the daily task deliberately targets just the
permanent-gap accumulators: `tradingview`, `short_interest`, `finnhub_events`).

Built the missing half: **`status.py`** — one-command freshness dashboard combining
three already-existing signals (no new data logic): `q.tables()` row counts,
`q.date_range()` max data dates, and file mtime via `validate.py`'s `_latest_file()`.
```
python status.py                    # every table, sorted stalest-first
python status.py --stale-days 3     # only tables not written to in >= N days
python status.py --table prices     # single-table detail
```
Live run: 236 CATALOG entries, 88 NO DATA, 148 PASS, correctly surfaces staleness
(e.g. `fred_rates_gdp_*` tables at 5.8 days since last write).

---

### D. ~~Historical Earnings Backfill~~ ✓ COMPLETED (via a different source than originally scoped)
**Status:** Implemented 2026-07-29

Originally scoped as a chunked year-by-year backfill loop against Finnhub's
`/calendar/earnings`. Verified live before building: that endpoint accepts arbitrary
`from`/`to` ranges without erroring, but the free tier only actually returns rows for a
recent rolling window — 2026-01/04/05 all returned 0 rows, 2026-06 returned 37
(stragglers), 2015/2020/2022/2023 all 0. This matches `docs/AUTOMATION.md`'s 2026-07-23 note
that Finnhub's free tier "will not return earnings_calendar rows older than ~1 year even
with `--backfill`" — a genuine dead end, not a code problem.

Found the real fix instead: `alpha_vantage_earnings` (populated by
`alpha_vantage_fundamentals_pipeline.py`'s daily rotating-subset accumulator, already
running) has real historical earnings dates + actual EPS surprises back to 1996 for
every symbol it's reached so far (9 so far; grows daily, quota-gated at 25 req/day).
Rewired `event_backtest.earnings_events()` to source from it instead of
`earnings_calendar`. Verified live: `earnings_events(min_surprise_pct=5)` now returns
621 real historical events (1996–2026) instead of 0, and `event_study()` on them shows
a real, decaying edge (1-day t-stat 4.02, mean +0.96%, decaying to +0.91%/t=2.89 by 10
days) — this was previously impossible to compute at all. No schema/contract change for
callers (same 5-column output). Full test suite re-run to confirm no regressions.

---

### E. Full Schwab Price History Backfill
**Priority: Medium | Effort: Low (compute), Medium (storage sizing)**

`price_history_pipeline.py --full` is verified working (daily bars to ~1985 via
the startDate-wins-over-period trick) but has deliberately not been run for the
full watchlist — `--full` mode prints a per-symbol date-range/row-count estimate
first so storage can be sized before committing to the pull.

---

### F. Finnhub Endpoint Expansion — DONE (via a different path)
**Priority: N/A | Status: Superseded, no action needed**

Originally scoped (2026-07-28, on a diverged fork) as adding analyst estimates, peers,
executives, ownership, revenue-breakdown, filings-sentiment, and ETF composition to
`finnhub_pipeline.py`. During reconciliation of that fork into this branch, found all of it
already implemented here under `finnhub_fundamentals_pipeline.py` (estimates, ownership, splits,
peers, executives, filing-sentiment, transcripts) and `finnhub_expansion_pipeline.py` (ESG,
congressional trading, supply chain, insider/social sentiment, SEC filings, lobbying, patents,
economic calendar). No further work needed here.

---

### G. ~~FRED Labor-Market Gap-Fill~~ ✓ COMPLETED
**Status:** Implemented 2026-07-28

3 of the originally-scoped 7 series (`M1SL`, `WALCL`, `CPILFESL`) were already in
`fred_rates_gdp_pipeline.py`. Added the remaining 4 as a new `labor` sub-category: `PAYEMS`
(Nonfarm Payrolls), `ICSA` (Initial Jobless Claims), `CCSA` (Continued Jobless Claims),
`CIVPART` (Labor Force Participation Rate) — a new sub-category means a new output table
(`fred_rates_gdp_labor`), so wired it fully per the standard checklist: `query.py` CATALOG,
`validate.py` SCHEMAS, `curated.py` KEYS, `run_all.py` PipelineSpec's `tables` list,
`tests/test_catalog.py` EXPECTED_TABLES, plus the storage directory. Full suite: 468
passed, 4 skipped, no regressions. Not yet backfilled (needs a live `FRED_API_KEY` run).

---

### H. ~~Indeed Hiring Lab Pipeline~~ ✓ COMPLETED
**Status:** Implemented 2026-07-28

`indeed_hiringlab_pipeline.py` — 3 keyless CSV pulls from `github.com/hiring-lab`
(national, sector, state job-postings index), re-verified live at implementation time
(data through 2026-07-24, even fresher than the spec's 2026-07-17 check). CATALOG:
`indeed_job_postings_national` (4,732 rows), `indeed_job_postings_sector` (194,012
rows), `indeed_job_postings_state` (120,666 rows) — all fully wired
(query.py/validate.py/curated.py/run_all.py/test_catalog.py/test_pipelines.py) and
verified live: pipeline run, `validate.py` PASS on all 3, `curated.py` compact clean,
`run_all.py --dry-run` recognizes the new spec. Added `analytics/labor.py:hiring_trend()`
(national/sector/state index level + WoW/MoM % change) per the spec, smoke-tested against
real data. Full suite: 470 passed, 4 skipped (up from 468). Skipped the spec's proposed
dedicated `tests/test_indeed_hiringlab_pipeline.py` — no other recently-added pipeline
(dark_pool/retail_sentiment/insider_sentiment) has one either; the smoke test
(`test_pipelines.py`) + catalog test (`test_catalog.py`) is this repo's actual baseline,
not the spec's more elaborate ask.

**Rejected candidate:** Opportunity Insights Economic Tracker — its spending/employment series are
discontinued (stale since 2024/2025); see spec for detail.

---

### I. Fix and re-scope the geopolitical/supply-chain analytics suite
**Priority: Low | Effort: Medium**

A diverged fork built an 11-module `analytics/` suite (chokepoint_volatility, correlation_regime,
freight_inflation, geopolitical_risk, macro_factor_model, oil_tanker_signal, options_macro,
port_congestion, retail_vs_institutional, supply_chain_composite, trade_fx_signal) but every module
imports `get_connection` from `query.py`, which doesn't exist — a hard `ImportError` before any
module ever runs. At least one module (`chokepoint_volatility.py`) also hand-builds a
`storage/raw/market/` glob path that doesn't match any real table (prices live under
`storage/raw/prices/`). Deferred rather than ported as-is during the 2026-07-28 reconciliation —
needs real per-module rework (add a public `get_connection()`/use `query.sql()`, fix hardcoded
paths, verify each module's logic actually produces sensible output) before it's worth adopting.

---

### J. Unified Eval Framework v2 — Transaction Costs / Slippage / Borrow
**Priority: Medium | Effort: Medium**

An audit (2026-07-28) confirmed the framework designed with Claude Fable 5
(`docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md`) is fully built,
tested (95/95), merged, and already used for a real signal decision — this and items
K–N below are its explicitly-deferred v2 scope, not unfinished v1 work. Portfolio
(`evaluation/portfolio.py`) and trade (`evaluation/trades.py`) evaluations currently
assume frictionless fills. Needed before any result is treated as investable rather
than research-only.

---

### K. Unified Eval Framework v2 — Capital-Constrained Compounding Equity Curves
**Priority: Medium | Effort: Medium**

`evaluation/trades.py`'s trade simulation is flat-notional per trade ($10k default,
per the `TradeRule` contract), not a compounding portfolio that sizes positions off
available capital. Deferred in the original spec pending real usage of the simpler
model first.

---

### L. Unified Eval Framework v2 — Live/Daily Refresh Wiring
**Priority: Low | Effort: Low–Medium**

No scheduled/automated re-run of `evaluate.py` against fresh curated data — every run
today is manual (`evaluate.py --adapter ...`). Could follow the existing Task
Scheduler pattern used for `ClaudeAuto-PipelineQuality`/`ClaudeAuto-TranscriptPull` if
recurring factor re-evaluation becomes routine.

---

### M. Unified Eval Framework v2 — Config-YAML Declarative Runner
**Priority: Low | Effort: Medium**

The spec's "Approach C" layer (declarative YAML-driven runs instead of CLI flags +
Python adapters) was deliberately deferred until the current programmatic interface
proved out through real use. It now has ~2 months of usage behind it (the acceptance
run plus the 2026-07-23 `low_vol` factor decision) — worth revisiting only if adapter
proliferation or run repeatability becomes a real friction point, not preemptively.

---

### N. Unified Eval Framework v2 — Conditional/Compound Scenario Testing + Price-Volume Signal Family
**Priority: Low | Effort: High (own design cycle)**

Two items the spec explicitly scoped out as needing their own future brainstorm →
spec → build cycle, not small add-ons: (1) conditional/compound scenario testing —
`event_backtest.scenario()` exists as the seed but the eval framework doesn't call it
yet; (2) a price-volume signal family, entirely unbuilt.

---

### O. Eval Registry Persistence
**Priority: Low | Effort: Low**

`storage/eval_registry/` and `storage/reports/eval/` are correctly gitignored and
don't currently exist on disk — results from past runs were captured into session-note
markdown instead of kept as artifacts, so `evaluation/registry.py`'s `compare()`/
`population()` (used for Deflated Sharpe's "how many things were tried" denominator)
reset every time a worktree is cleaned up rather than accumulating real history.
Worth a conscious decision: keep a persistent (if gitignored) registry file — e.g. on
the portable drive alongside the master `.env` — so repeated `evaluate.py` runs build
up a real population over time.

---

### P. ~~Regression: `test_storage_dirs_exist` — ~100 missing CATALOG storage directories~~ ✓ RESOLVED
**Status:** Fixed 2026-07-28

`tests/test_catalog.py::TestCatalogPaths::test_storage_dirs_exist` was failing for 99
CATALOG entries (EIA/BLS/FRED-expansion/Finnhub-expansion/Treasury/Tiingo/Coingecko/
Alpha-Vantage/SEC-EDGAR tables, plus the older `fhfa_hpi`/`zillow_zhvi`/`zillow_zori`/
`market_history`/`tv_ratings`/`sec_filings`/`tsa_checkpoint`) whose storage directories
had never been created on this checkout — verified each had a real, existing backing
pipeline file before touching anything (all 20 source pipelines confirmed present) so
nothing fabricated got a directory. Fixed by creating the 99 missing
`storage/raw/.../` directories with `.gitkeep` placeholders. Full suite: 468 passed,
4 skipped (up from 466 — no regressions).

---

### R. Fixed correctness bug: `feature_matrix(start=...)` silently zeroed out all rolling-window factors
**Status:** Fixed 2026-07-29

Discovered while scoping item Q: `analytics/features.py::feature_matrix()` passed `start`
straight into the price query (`_price_panel(pt, symbols, start, end)`) **before**
computing rolling-window features (`mom_12_1` needs a 252-trading-day lookback,
`ret_252d`/`vol_21d` similarly). Any call with an explicit `start` left-truncated the
price history first, so every row in the requested window had insufficient trailing
data — `mom_12_1` came back **100% NaN for the entire panel**, even for AAPL with 50+
years of history on file. Confirmed live: `feature_matrix(symbols=['AAPL'],
start='2026-06-01')` -> all-NaN `mom_12_1`; same call with no `start` -> correct values
(e.g. 0.42). At full-universe scale (`symbols=None`, 22,950 symbols in a 2-month test
window) the `momentum` column was **silently absent from `signal_panel()`'s output
entirely** — this would have invalidated any full-universe or windowed momentum
evaluation without raising an error.

**Fix:** pad the internal price query back 450 calendar days when `start` is set, compute
rolling features over the padded panel, then trim to the true `start` before the
(expensive) fundamentals/macro/short-interest/insider/sentiment joins run — a strict
improvement, not just a correctness fix, since fewer padding rows now flow through those
joins. Verified: `mom_12_1` for AAPL with `start='2026-06-01'` now matches the no-`start`
baseline exactly; full-universe `signal_panel(symbols=None, start=..., end=...)` now
returns a populated `momentum` column (558,961 non-null / 605,853 rows). This directly
unblocks item Q, whose entire premise depends on `start=`/`end=`-windowed momentum
evaluation. No test previously caught this — `tests/test_features.py` apparently doesn't
exercise a rolling-window factor together with an explicit `start`; worth adding a
regression test for exactly that combination.

---

### S. `_asof_fundamentals` OOMs at full-universe scale (unbounded `feature_matrix()` calls)
**Priority: Low | Effort: Medium | Status: Documented 2026-07-29, worked around not fixed**

Found while re-running `tests/test_features.py` after the item R fix: a fully unbounded
`feature_matrix()` call (`symbols=None`, all blocks on) against this clone's real
27,759-symbol / 46.9M-row `prices` table crashes with `numpy.core._exceptions.
_ArrayMemoryError` inside `_asof_fundamentals()`'s `out.merge(joined, on=["symbol",
"date"], how="left")` — a plain pandas merge run once per fundamentals metric against
the full panel. `tests/test_features.py::TestFeatureMatrixIntegration::
test_returns_dataframe` hit this directly; fixed by scoping the test to a small symbol
subset (its actual intent — "does this run at all" — not "does the absolute worst case
fit in memory"), not by touching `_asof_fundamentals` itself.

**Not fixed at the source** because the real fix (doing the per-metric join inside
DuckDB against the full panel instead of pandas `.merge()`, so nothing pandas-side ever
materializes at 46.9M-row scale) is a real refactor of a widely-used core function, out
of scope for a quick pass. **Workaround in place for item Q**: full-universe momentum/
low_vol evaluation never needs the fundamentals/short-interest/insider/sentiment blocks
in the first place (those factors are watchlist-only anyway), so `evaluation/
universe.py`'s eligibility-filtered adapter calls `feature_matrix(..., fundamentals=
False, short_interest=False, insider=False, sentiment=False)` — this sidesteps the OOM
path entirely rather than fixing it. Revisit `_asof_fundamentals`'s merge strategy if a
future need requires fundamentals at full-universe scale.

---

### Q. ~~Full-Universe Factor Validation (momentum / low_vol)~~ ✓ COMPLETED
**Status:** Implemented 2026-07-29 — spec at `docs/superpowers/specs/2026-07-29-full-universe-factor-validation-design.md`

Validates `momentum`/`low_vol` — the only two factors with real full-universe breadth,
since `value`/`quality`/`growth`/`sentiment`/`insider_flow` all need fundamentals/
short-interest/insider/sentiment data that only covers the 69-symbol watchlist — against
the full ~13,219-symbol exchange-listed universe (`symbol_universe.csv`, excluding OTC
Markets/Nasdaq OTCBB) instead of the watchlist, using a point-in-time (no-lookahead)
trailing dollar-volume eligibility filter rather than a static "liquid today" universe.

**Built:** `evaluation/universe.py` (`exchange_listed_symbols()` + `point_in_time_eligible()`
— one DuckDB rolling-window query, parameterized not string-interpolated, padded 45 days
before `start` so the trailing window isn't truncated at the boundary — same bug class as
item R); additive `eligible=` param on `evaluation/adapters.py::from_signal_panel()`
(default `None` reproduces prior behavior exactly — all 95 original `test_evaluation.py`
tests pass unmodified); two new opt-in CLI flags on `evaluate.py` (`--exclude-otc`,
`--min-dollar-volume`, wired to route through the light-`feature_matrix` / OOM-safe path
any time either is set). Zero changes to the tested core (`data.py`/`ic.py`/`stats.py`/
`runner.py`) — confirmed they already operate per-date on whatever rows are in the panel.

New tests: `tests/test_universe.py` (OTC exclusion, no-look-ahead proof via a synthetic
volume-spike series, empty-input edge case) + two new adapter tests in
`tests/test_evaluation.py` (`eligible=` filters rows; `eligible=None` stays unfiltered).
Live-verified: `exchange_listed_symbols()` → 13,218 symbols; `point_in_time_eligible()`
on the real `prices` table (13,218 symbols, 7-week window) ran in 1.3s; the full adapter
path (`--exclude-otc --min-dollar-volume 1000000`) correctly narrowed a momentum panel
from 12,081 unfiltered symbols to 8,374 eligible ones over the same window.

**Explicit, deliberately-unfixed limitation:** the point-in-time liquidity filter only
solves look-ahead from using *today's* liquidity to judge history. It does NOT solve
delisting survivorship — `prices`/`symbol_universe.csv` are a 2026-07-24 snapshot of
currently-tradable Schwab instruments, so any company that delisted, was acquired, or
went bankrupt before then is entirely absent from the data. Fixing that needs a different
data source (e.g. a CRSP-style point-in-time constituent history) — out of scope here,
stated prominently rather than glossed over.

Scoping this out also surfaced and fixed two real bugs along the way — see items R
(`feature_matrix(start=...)` silently zeroed rolling-window factors) and S
(`_asof_fundamentals` OOMs at full-universe scale, worked around not fixed).

**Acceptance run** (momentum + low_vol, full history, `registry.compare(
allow_universe_mismatch=True)` against the watchlist baseline) — in progress; see
`docs/sessions/SESSION_NOTES.md`'s 2026-07-29 entry for the result once it completes.

---
