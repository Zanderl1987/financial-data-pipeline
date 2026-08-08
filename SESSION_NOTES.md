# Session Notes — running log

## 2026-08-02 — session 2, part 2: etf_holdings orphan wired in + CLAUDE.md/requirements/task cleanup

- **etf_holdings orphan resolved (user: wire it into CATALOG).** Was the only HF table (of
  152) with no CATALOG entry — provenance: a one-off snapshot from SecuritiesDB's free
  keyless API (top-100 holdings/ETF with Piotroski F / Altman Z / market_cap / sector),
  leaked onto HF because `upload_huggingface.py`'s `upload_folder` sweeps
  `storage/curated/**` and never deletes. Built `etf_holdings_pipeline.py` (119-fund
  universe from the HF snapshot's tickers + embedded ETF_NAME_MAP, retry/backoff, 0.4s
  throttle) and wired it fully: CATALOG `_glob("etf_holdings/**/*.parquet")`, validate
  SCHEMAS (weight_pct (0,100), piotroski_f (0,9)), run_all PipelineSpec stage 1
  (timeout 600), curated KEYS (fund_ticker/holding_ticker/snapshot_date), both test
  lists, docs/PIPELINE_CATALOG.md. Verified: live pull 210 rows/3 funds, validate PASS
  0/0, curated 0 dupes, run_all --dry-run picks it up, full suite **446 passed / 18
  skipped** (was 444).
- **CLAUDE.md de-stale'd (TASKS item):** 133→**235 CATALOG tables**, 273→**444 tests**,
  python path `C:\ProgramData\anaconda3\python.exe`→`C:\Users\Zander\AppData\Local\Programs\Python\Python312\python.exe`
  (verified: anaconda gone, Python312 live, bare `python` now resolves to Python312 not a
  MS Store stub), repo root `C:\Users\zande\PycharmProjects\...`→`C:\Users\Zander\financial-data-pipeline`
  (verified on disk). All command examples updated.
- **CATALOG category headers added (TASKS item):** the ~93-headerless block
  (`fred_macro_` → `treasury_mts_budget_comparison`) now has `# -- <category> (extended) --`
  headers matching lake_manifest.py's prefix-fallback labels, so the source (not the
  parser) owns categorization. Verified: `parse_categories()` returns 0 "unlabeled in
  source" entries across all 235 tables.
- **requirements.txt fixed (TASKS item):** added `sqlalchemy==2.0.51` (pyiceberg
  sql-sqlite extra) + `xlrd==2.0.2` (GSCPI Excel), both verified installed.

**Still open as of end of this part:** HF 152-config rebuild — background poller had been
busy-500 for ~45+ min (README configs YAML landed 01:49Z; per-config convert branch
appears only at completion, still absent). Re-poll `/splits` until `ready=152/pending=0`.

## 2026-08-02 — session 2, part 3: etf_holdings full-universe run + merge + HF push

- **Full 119-fund universe run** of `etf_holdings_pipeline.py` completed: 7,539 rows
  (08-02 snapshot), validated PASS 0/0, curated 0 dupes.
- **Merged with existing HF history rather than overwriting** (fresh pull was *smaller*
  than HF's 7,723: holdings lists drift between snapshots). Pulled
  `etf_holdings/etf_holdings.parquet` (07-30: 7,524 + 07-31: 199), concat + dedup on
  (fund_ticker, holding_ticker, snapshot_date) → **15,262 rows / 3 snapshot dates / 119
  funds**, wrote to curated, validated PASS 0/0.
- **Pushed to HF** via `upload_file` (242kB, replace existing) — commit msg
  "etf_holdings: merge 08-02 fresh pull with 07-30/07-31 history (15,262 rows, 119
  funds)". HF repo path is `etf_holdings/etf_holdings.parquet` (NOT `data/...` — 404'd on
  first attempt; used `api.list_repo_files` to find the real path).
- **README re-uploaded**: row total 96,973,945 → **96,981,484** (text-only change; the
  152-entry configs YAML block untouched, so the in-flight per-config rebuild is
  unaffected). Re-upload happened while rebuild still busy.
- **CLAUDE.md synced**: 236 CATALOG tables / 446 tests (etf_holdings wired in).

## 2026-08-02 — Redfin + AQR factor pipelines; HF multi-config viewer fix; last 2 local tables pushed

Two new keyless pipelines (Redfin housing tracker, AQR factor library) built, wired, and
verified end-to-end; HF dataset finished at 152 tables / 96,973,945 rows; HF viewer
one-config problem fixed via README configs YAML (rebuild still running in background).

**HF last-2-tables push:** `cfpb_complaints` (6.7M) and `open_meteo_weather` (43K) were the
only local tables missing from HF (landed after the 2026-07-30 publish). Pushed via temp
`push_missing_tables.py` (`api.upload_folder` per table) → 152 parquet tables / 96,973,945
rows on HF (row total arithmetic re-verified: 90,223,873 + 6,706,597 + 43,475).

**HF viewer / one-config fix (in progress):** diagnosed that HF auto-conversion had merged
ALL 152 files into one config (`refs/convert/parquet/default/train`) — that's why the
viewer showed everything as one dataset. Fix: added a 152-entry `configs:` YAML block to
the README front matter (generated from the 152 repo parquet files), rewrote the usage
example to `load_dataset(repo, "<table_name>")` + `ds["train"]`, bumped counts to 152
tables / 96,973,945 rows, uploaded. datasets-server picked it up and began a per-config
rebuild — the `refs/convert/parquet` branch now builds one folder per config (observed
111/152 at one point) instead of a single `default`. Still processing in background.
**Polling note:** `/configs` is a dead endpoint now (404s even on known-good datasets, don't
poll it); `/splits` returns busy-500 while processing and `ready=<n> / pending=<m>` when
settled — those are normal states, not failures. Verify `ready=152 / pending=0` before
calling this done.

**Data source research (task 4):** confirmed pipeline already covers GSCPI, World Bank,
Zillow, Fama-French, USGS — no overlap. New buildable sources: Redfin (live
`redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/` TSV.gz files) and
AQR factor library (live `.xlsx` at `aqr.com/-/media/AQR/Documents/Insights/Data-Sets/`).
Skipped: IEA EV battery prices (403-gated .Stat), MSRB EMMA (paid), FINRA TRACE
(registration-gated; not user-approved to build).

**Redfin pipeline (`redfin_pipeline.py`):** downloads national/metro/state by default
(`--granularity all` adds county/city/zip_code; `--only <level>` for one), parses quoted
TSV (58 cols), lowercases columns, DATE_COLS→datetime + numeric casts, drops null
period_begin/region, adds `region_level` + `fetched_at`, writes via `write_partitioned`
to `storage/raw/redfin/market_tracker/`. Live result: 624,234 rows (national 1,903 /
metro 579,544 / state 42,787), 2012-01 → 2026-05.

**AQR factor library (`aqr_factors_pipeline.py`):** downloads VME/QMJ/TSMOM xlsx, locates
header row (first row whose first non-empty cell is a non-numeric label followed by a
parseable date), melts wide→long `(date, source, factor, value)`, `--backfill` for full
history. **TSMOM quirk:** its header row has no label in the date column (first cell None)
and both workbooks carry trailing pad cells — final fix uses first-non-empty-cell header
detection, normalizes column 0 to `date`, and renames other blank headers `__padN__`
(dropped after melt). Live result: 27,048 rows (VME 13,243 / QMJ 11,320 / TSMOM 2,485),
56 factors, 1957-07 → 2026-05. Backfill parquet filename is stale
(`aqr_factors_backfill_20260802.parquet` predates the TSMOM fix) but contains all three
sources — safe to regenerate or leave.

**Wiring (both pipelines):** `query.py` CATALOG (`_glob("redfin/market_tracker/**/*.parquet")`
+ `_glob("aqr/factors/**/*.parquet")`), `validate.py` SCHEMAS (`redfin_market_tracker`
period_begin/region/property_type + value_ranges; `aqr_factors` date/source/factor/value
modeled after `ff_factors`), `run_all.py` PipelineSpec stage 1 (`redfin` timeout 1200,
`aqr_factors` timeout 600), `curated.py` KEYS (`redfin_market_tracker` →
period_begin/region/property_type/is_seasonally_adjusted; `aqr_factors` →
date/source/factor), `tests/test_catalog.py` EXPECTED_TABLES, `tests/test_pipelines.py`
PIPELINE_MODULES, `docs/PIPELINE_CATALOG.md` rows.

**Verification:** full suite **444 passed / 18 skipped**; `validate.py` both PASS (0
errors); widened `months_of_supply` range to (0,200) — the 2 flagged values were real
Vermont data (131/167 months of supply in early 2015), not parse errors; curated dedup
clean (0 rows removed); `query.py` reads both tables; `run_all.py --dry-run --stage 1`
picks both up.

**Open:** HF config rebuild still running — poll `/splits` until `ready=152/pending=0`;
re-upload README if row totals change with new tables.

## 2026-08-01 (session 2) — negative-price guard + HF constituents/shipping refresh

Set out to (1) refresh shipping/constituents data on HF and (2) guard against the
Schwab additive-dividend-adjustment negative-price bug flagged in PROJECT_NOTES.md.
Both done, but the actual work diverged a lot from the plan once live state was checked.

**Negative-price guard:** `close<=0` alone wasn't enough — a synthetic COST test (mimicking
the 2009-03-09 crossing) showed the day *after* crossing still had a negative `low` and a
garbage `pct_change` (-304%). Guard in `curated.py`'s `_compact_large_table()` now computes,
per symbol, the last date where ANY of open/high/low/close <= 0, and nulls open/high/low/
close/pct_change/log_return/intraday_change/intraday_range/vwap through that date (volume/
fetched_at untouched). Applied one-time to the live published `prices` table: 896,583 of
46,950,543 rows affected, 0 remaining bad rows after, all 27,759 symbols intact.

**HF sync reality check:** PROJECT_NOTES said `prices`/`cfpb_complaints`/`open_meteo_weather`
were "not yet synced" — checked live and they already were (that note was stale). The
genuinely stale table was `index_members` (9 days old, and no fresher local copy existed to
push — this Passport-drive checkout's local Iceberg store for constituents/shipping was
empty). Ran `index_constituents_pipeline.py` fresh (7,587 rows) and `shipping_pipeline.py`
(gscpi full-history, matched HF exactly at 342 rows; freight_ppi incremental-only, 26 rows).

**The catch that mattered most:** before uploading anything, compared every local curated
table's size against its HF counterpart. **9 tables would have been silently gutted by a
blind `upload_huggingface.py` run** — this checkout's `storage/curated/` is a partial subset
(708MB) of the full published dataset. Worst cases: `fundamentals_quarterly` would have
dropped to 0.4% of its published size, `fundamentals_annual` to 0.8% (`finnhub_news`,
`news_sentiment`, and 5 others also affected). `upload_folder()` does a straight per-path
file replace with no merge/delete_patterns logic. **Did not run the folder-wide upload.**
Instead: merged `index_members` and `shipping_freight_ppi` with their existing HF history
via DuckDB (UNION + keyed dedup, not overwrite) before pushing, and used targeted
`api.upload_file()` calls for only the 4 verified files (`prices`, `index_members`,
`shipping_gscpi`, `shipping_freight_ppi`). The other ~140 local tables were left untouched.

**Blockers hit and fixed:**
- `pyiceberg`, `sqlalchemy` (pyiceberg's sql-sqlite extra), and `xlrd` (GSCPI Excel parsing)
  were all missing from `requirements.txt` — installed by hand, not yet added to the file.
- The Iceberg catalog stale-path bug from `SESSION_NOTES_2026-07-18-constituents.md`
  (`metadata_location` pointing at `C:\Users\zande\PycharmProjects\...`) had recurred on
  this Passport-drive copy of `constituents_catalog.db`. Fixed by dropping + recreating
  `constituents.index_members`, `shipping.gscpi`, `shipping.freight_ppi` (confirmed empty
  locally first, so no data lost). `securities`/`fund_holdings`/`identifier_map` still have
  the same stale pointer — not fixed, since HF already has current data for those and
  nothing this session needed to write to them.

**Decision:** `upload_huggingface.py` is not safe to run as-is from this checkout until
either it gains a size-sanity/merge-aware guard, or this checkout gets a full local backfill
first. Don't run the folder-wide upload blind again — check local-vs-remote sizes first.

## 2026-08-01 (session 1) — data lake manifest exporter + interactive dashboard

Built `scripts/lake_manifest.py`: exports a single JSON manifest of the data lake
(category, row count, schema, size, date range, freshness, source pipeline) for every
CATALOG table. Then built and published an interactive dashboard artifact (Structure &
Size treemap, Schema & Relationships browser + join graph, Lineage flow, Freshness) on
top of it.

**Findings, not obvious from the code alone:**
- This checkout's `storage/raw` is code-only — e.g. `storage/raw/prices` is just a
  `.gitkeep`. The actual populated snapshot (150 tables, 90,223,873 rows, 2.68 GB) lives
  only on the Hugging Face mirror (`ZanderL1337/financial-data-pipeline`), pushed by
  `upload_huggingface.py` from wherever the pipelines actually ran. The manifest script
  pulls live row counts/schema/size/date-range from the HF-hosted parquet footers via
  DuckDB httpfs (no full download) rather than querying local `query.py`.
- `query.py`'s CATALOG dict has **233 tables**, not the 133 CLAUDE.md states — that line
  is stale (last verified 2026-07-07).
- 93 of those 233 tables (`signal_health` onward through `cfpb_complaints`) have **no
  category comment header** in the source — a naive parse dumps them all under
  "Signal health monitor...". Worked around with a name-prefix fallback in the export
  script; the source file itself is still uncommented for that block.
- `etf_holdings` exists on HF but isn't in the current CATALOG — likely renamed or
  retired since the last upload; not investigated further.

**Decision:** re-run `scripts/lake_manifest.py` any time storage/HF changes, then
republish the same artifact path to refresh the dashboard.

## 2026-07-20 (session 3) — FRED shipping expansion (+10 series)

Expanded `FREIGHT_SERIES` in `shipping_pipeline.py` from 8 to 18 series, backfilled, curated, validated, tests pass (145/145).

**New series added:**
| Series ID | Description | History |
|-----------|-------------|---------|
| FRGSHPUSM649NCIS | Cass Freight Index: Shipments | 2016-01 to 2026-06 |
| FRGEXPUSM649NCIS | Cass Freight Index: Expenditures | 2016-01 to 2026-06 |
| TRUCKD11 | Truck Tonnage Index (ATA) | 2000-01 to 2026-04 |
| RAILFRTCARLOADSD11 | Rail Freight Carloads SA | 2000-01 to 2026-04 |
| RAILFRTINTERMODAL | Rail Freight Intermodal Traffic NSA | 2000-01 to 2026-04 |
| PCU483211483211 | Inland Water Freight PPI | 1990-12 to 2026-06 |
| PCU481112481112 | Scheduled Freight Air PPI | 2003-12 to 2026-06 |
| IC131 | Inbound Air Freight Price Index | 1990-09 to 2026-06 |
| IS231 | Outbound Air Freight Price Index | 1992-09 to 2026-06 |
| AIRRTMFMD11 | Air Revenue Ton Miles (SA) | 2000-01 to 2026-03 |

**Curated `shipping_freight_ppi`:** 5,282 rows across 18 series.

## 2026-07-20 (session 2) — shipping data source research

Researched free/cheap shipping data sources to expand the pipeline beyond GSCPI + 8 FRED series.

**Tier 1 — FRED expansion (zero new infra, existing FRED_API_KEY):**
- Cass Freight Index: Shipments (`FRGSHPUSM649NCIS`) and Expenditures (`FRGEXPUSM649NCIS`) — most-watched US freight volume proxies, monthly 2016+
- Truck Tonnage Index (`TRUCKD11`) — ATA data, monthly 2000+
- Inland Water Freight PPI (`PCU483211483211`) — monthly 1990+
- Air freight price indexes — Inbound (`IC131`, 1990+) and Outbound (`IS231`, 1992+)
- Rail Freight Carloads and Intermodal Traffic — monthly 2000+
- Air Revenue Ton Miles of Freight and Mail — monthly 2000+
- Scheduled Freight Air Transportation PPI (`PCU481112481112`) — monthly 2003+
- Regional diesel prices (`GASDESECM` etc.) — PADD-level, beyond national average

**Tier 2 — FreightPulse (new source, free tier):**
- 100 calls/month free, no credit card needed. Port congestion, freight rates, fuel prices, disruption alerts.
- Snapshot data only (no history backfill).
- Signup: https://freightpulsehq.com/

**Tier 3 — ShippingRates (no signup basics):**
- 25 free requests/month, zero signup. Port-to-port freight rates, carrier tariffs, congestion.
- Paid after 25 via crypto micropayment.
- Not time-series — route queries.

**Tier 4 — Zemlo AI (zero-auth):**
- No API key needed. Live carrier rates + risk + CO2 per route.
- `/signal?from=X&to=Y` at https://zemloai-api.onrender.com
- Case-by-case, not time-series. No history.

**Decision:** Start with Tier 1 FRED expansion (Cass, truck tonnage, water/air freight, rail, regional diesel) since it's keyless and immediate. Then add Tier 2 FreightPulse if Zander signs up for a free key.

**Executed 2026-07-20 (session 3):** Tier 1 complete. 10 new FRED series added, backfilled, curated. 18 total series in `shipping_freight_ppi`.

**Iceberg migration (session 3 cont.):** Rewired shipping pipeline to write Apache Iceberg tables with Snappy compression. Created `shipping.gscpi` and `shipping.freight_ppi` Iceberg tables (shared catalog `constituents_catalog.db`, namespace `shipping`). Backfilled: 342 rows GSCPI, 5,282 rows freight_ppi — all Snappy-compressed. Updated `query.py`, `validate.py`, `curated.py`. Validate PASS, 145/145 tests pass.

## 2026-07-18 — constituents Iceberg cleanup + NDX scraper fix + visualizations + identifier_map

Revisited the `index_constituents_pipeline.py` Iceberg table. Fixed three issues:

1. **Iceberg table stale paths.** SQLite catalog had `metadata_location` pointing to
   `E:/AI_Projects/...` (wrong drive). Dropped table, deleted directory, recreated with
   `catalog.create_table()` using correct C: drive paths. Schema fix: `TimestamptzType`
   (not `TimestampType`) for `fetched_at` to match Arrow `pa.timestamp("us", tz="UTC")`.
2. **NDX scraper no company names.** stockanalysis.com HTML has SvelteKit hydration
   comments between `<td>` elements. Added `re.sub(r'<!----?>|<!--.*?-->', '', text)`
   comment stripping, then paired regex for `<td class="sym">` + `<td class="slw">`.
   NDX now returns 103 tickers with company names.
3. **Visualizations.** Generated 5 matplotlib charts (sector pie/bar, index sizes, overlap
   matrix, R2K top holdings) from DuckDB queries against the Iceberg table. Saved to
   `storage/iceberg/viz/`.
4. **identifier_map enriched.** Expanded from 10 rows (mega-caps with FIGI) to 3,056 rows
   (all index_members tickers with CIK from SEC EDGAR via securities table). 99% CIK
   coverage. Used drop+recreate to avoid stale parquet file issue with overwrite.
5. **Iceberg health check.** All 4 tables healthy. fund_holdings has 25 snapshots that
   could be expired. securities is clean (10,426 rows, 100% CIK).

Detail: `SESSION_NOTES_2026-07-18-constituents.md`. TODOs: OpenFIGI key registration,
full OpenFIGI run, disk size assessment, fund_holdings snapshot expiry.

## 2026-07-16 — verification pass + commodity build (session 8)

Attempted to fix 9 items from the prioritized build table. All 8 code fixes (items 1-8)
were already implemented in sessions 5-6 and committed in session 7 (`a0b78b0`). Verified
each finding against current code:

- eia_hourly_grid: fully wired (validate.py:1110, run_all.py:620, curated.py:159,
  test_pipelines.py:72, test_catalog.py:121)
- finnhub dedup keys: natural keys `['symbol', 'date']` / `['symbol', 'id']`
- treasury_fiscal pagination: reads `total_pages`/`total-pages` correctly
- alpha_vantage dividends: reads `data.get("data", [])` correctly
- coingecko null crash: `or {}` guard on lines 179, 202
- exposure.py OLS: uses `np.linalg.pinv` (pseudo-inverse)
- features.py tie-break: breadth-based `(overlap, len(syms))`, backtest.py passes symbols
- event_impact.py except: catches only `NoQualifyingEventsError` (narrow subclass)

Commodity build completed: 25 FRED PPI series (lumber/steel/plastics/glass) added to
`commodity_macro_pipeline.py`, LBR=F + HRC=F added to `futures_pipeline.py`.

Full test suite: 313/313 pass (0 failures). Item 9 (options analytics staging merge)
still pending.

## 2026-07-16 — analytics/options.py repair (implemented)

Both functions in `analytics/options.py` were completely broken (KeyError on
camelCase yfinance columns). Rewrote per approved spec from 07-12:

- `put_call_ratio` → volume-based from `options_history` (was open-interest-based).
- `iv_summary` → sources `schwab_options` (preferred) → `options_chain` (fallback),
  with column normalizer. Returns empty today (no Schwab OAuth data).
- 10 new behavior tests, all passing. Full suite 309/310 (1 pre-existing fail).
- Live verified: `put_call_ratio("PLTR")` returns 441 rows of real data.

Files: `analytics/options.py`, `tests/test_analytics.py`.
Status: uncommitted, ready for review.
Detail: `SESSION_NOTES_2026-07-16.md`.

## 2026-07-16 — options analytics expansion design (session 2)

Designed comprehensive options analytics suite: 19 new functions expanding
`analytics/options.py` from 2 to 21 functions. Group I (13 functions, works NOW)
covers volume analytics, structural metrics, realized vol, Greeks. Group II
(6 functions, activates on Schwab OAuth) covers IV surface, skew, term structure.
Spec at `docs/superpowers/specs/2026-07-16-options-analytics-expansion-design.md`.

## 2026-07-16 — options analytics expansion: staging implementation (session 3)

Zander approved implementation with staging-only workflow: all code written to
`E:\AI_Projects\FinancialPipelineStagingUpdates\` — nothing touched C: drive repo.

**Group I (13 functions) — implemented in staging:**
Volume: volume_skew, unusual_volume, volume_by_strike, term_structure_volume,
volume_concentration, weighted_average_strike.
Structural: max_pain, put_call_parity.
Realized vol: realized_volatility, vol_regime.
Greeks: portfolio_greeks, gamma_exposure, theo_vs_market.

**Group II (6 functions) — implemented in staging:**
iv_surface, iv_skew, iv_term_structure, iv_rv_spread, unusual_activity,
vertical_spread_pricing. All return empty DataFrame today (Schwab OAuth pending);
tests use monkeypatched schwab_options data to verify logic.

**Staging files:**
- `E:\AI_Projects\FinancialPipelineStagingUpdates\analytics\options.py` — 1500 lines, 19 functions
- `E:\AI_Projects\FinancialPipelineStagingUpdates\analytics\__init__.py` — 74 lines, all 19 exports
- `E:\AI_Projects\FinancialPipelineStagingUpdates\tests\test_analytics.py` — 1355 lines, full test suite
- All files syntax-verified.

**Commit history this session:**
- `e52c6e0` — Fix analytics/options.py: rewrite put_call_ratio + iv_summary (repair)
- Expansion (Group I + II) pending Zander review, then merge into main repo.

## 2026-07-16 — commodity data source research + build (lumber, plastics, glass, steel)

Deep web research on free data sources for lumber, plastics, glass, and steel,
followed by implementation. Full audit of FRED API, yfinance, Commodities-API.com,
Metals-API, Investing.com, USGS, Trading Economics, PlasticPortal, Resintel,
ChemOrbis, Barchart, and IndexMundi.

**FRED API (best source — already wired):** 25 PPI series added to existing `SERIES`
dict in `commodity_macro_pipeline.py`. Lumber: WPU081, WPU0811, WPU0812, WPUSI012011.
Steel: WPU101, WPU1017, WPU1019A2S, PCU3259103259101, PCU3311103311101,
PCU3312223312221. Plastics: WPU066, WPU0662, PCU325211325211, WPU0653, WPU06.
Glass: PCU3272132721, PCU3272133272131, PCU3272143272141, WPU0619,
PCU3272153272151. All monthly, back 20-100 years. Zero friction.

**yfinance (added to futures_pipeline.py):** LBR=F (CME Lumber Futures),
HRC=F (CME HRC Steel Futures). Daily OHLCV, free. Added to existing FUTURES dict
as "industrial" category (28 → 30 contracts). No direct tickers for plastics or glass.

**Wiring:** No new table entries needed — FRED series flow into existing `commodities`
table, yfinance tickers flow into existing `futures` table. All 6 wiring files
(query.py, validate.py, run_all.py, curated.py, test_catalog.py, test_pipelines.py)
already cover these tables.

**Secondary sources (need sign-up, not built):**
- Commodities-API.com: LUMBER, SCRAP-HM, IRON_ORE symbols. Free 100 req/mo.
- Metals-API: LME Steel Rebar/Scrap/HRC. Free tier.
- USGS Mineral Commodity Summaries: Annual iron/steel stats. Free CSV/PDF.

**Glass gap:** No free spot price API exists for glass. No traded futures market.
PPI indices from FRED are the best freely available data.

**Tests:** 290/291 pass (1 pre-existing `eia_hourly_grid` fail). No regressions.

Files: `commodity_macro_pipeline.py`, `futures_pipeline.py`.

## 2026-06-29 — alternative data pipelines build

### 7 new alternative data pipelines (all Stage 1, free/keyless unless noted)

| Pipeline | File | Tables | Notes |
|---|---|---|---|
| Open-Meteo weather | `open_meteo_pipeline.py` | `open_meteo_weather` | 25 US locations, 11 daily vars, 1990+ |
| Wikipedia pageviews | `wikipedia_pipeline.py` | `wikipedia_pageviews` | 46 articles (DJI + macro), 2015+ |
| OpenFDA | `openfda_pipeline.py` | `openfda_approvals`, `openfda_recalls` | Drug approvals + enforcement recalls, 2010+ |
| Treasury TIC | `treasury_tic_pipeline.py` | `treasury_tic_holders`, `treasury_tic_slt` | Foreign holdings of US Treasuries by country |
| Google Trends | `google_trends_pipeline.py` | `google_trends_economic/market/sector` | 45 keywords, 3 groups, 5yr weekly backfill |
| Reddit sentiment | `reddit_pipeline.py` | `reddit_posts`, `reddit_mentions` | **Needs API keys** (see below) |
| AIS vessel tracking | `ais_pipeline.py` | `ais_positions`, `ais_zone_summary` | **Needs API key** (see below) |

All tables registered in `query.py` CATALOG and `validate.py` SCHEMAS.

---

## Backfill Status

### New pipelines
| Pipeline | Status | Rows |
|---|---|---|
| wikipedia | COMPLETE | 180,582 |
| openfda | COMPLETE | 18,104 approvals + 5,000 recalls |
| treasury_tic | COMPLETE | 12,355 holders + 8,664 SLT |
| google_trends | COMPLETE | 11,790 (3 groups x 3,930) |
| open_meteo | **IN PROGRESS** — batched rewrite running (background task `bo2t02z5d`) |
| reddit | SKIP — needs API keys |
| ais | SKIP — needs API key |

### Original pipelines (Run 2, completed)
26 PASS / 9 FAIL / 5 SKIP over 67 minutes. Failures:

| Pipeline | Failure | Investigated? |
|---|---|---|
| `futures` | exit 1 | No |
| `short_interest` | exit 1 | No |
| `coingecko` | timed out 600s | No — probably needs longer timeout |
| `prices`, `sector_etfs`, `schwab_*`, `options_chain` | exit 1 | Expected — no Schwab credentials |
| `synthetic_options` | timed out 1200s | Expected — depends on prices table |

---

## Open Issues / Next Steps

### 1. Confirm open_meteo batched run (immediate)
Background task `bo2t02z5d` is running the rewritten pipeline.
Old design: 25 API calls (blew quota). New design: 5 batched calls of 5 locations each, 45s pause between batches.
Once complete: verify 25 locations wrote, then `git pull` on D drive.

Check output:
```
C:\Users\Zander\AppData\Local\Temp\claude\C--Users-Zander\20eee7bc-4ce2-4758-affe-6bcd1da40cab\tasks\bo2t02z5d.output
```

### 2. Get Reddit + AIS credentials
**Reddit** — register a free "script" app:
1. Go to https://www.reddit.com/prefs/apps → "create another app" → type: script
2. Redirect URI: `http://localhost:8080`
3. Add to `.env`:
   ```
   REDDIT_CLIENT_ID=...
   REDDIT_CLIENT_SECRET=...
   REDDIT_USER_AGENT=financial-data-pipeline/1.0 (by u/your_username)
   ```
4. Test: `python reddit_pipeline.py --backfill`

**AIS** — free real-time vessel tracking:
1. Register at https://aisstream.io/ (no credit card)
2. Add to `.env`:
   ```
   AISSTREAM_API_KEY=...
   ```
3. Test: `python ais_pipeline.py --minutes 10`

### 3. Investigate futures + short_interest failures
Both exit 1 with no timeout. Not looked at this session. Run individually to see the error:
```
python futures_pipeline.py --backfill
python short_interest_pipeline.py --source all
```

### 4. Fix coingecko timeout
`coingecko_pipeline.py` timed out at 600s during backfill. Either:
- Increase its timeout in `run_all.py` (currently 600)
- Or run directly: `python coingecko_pipeline.py --backfill`

---

## Bugs Fixed This Session

| Bug | Root Cause | Fix |
|---|---|---|
| `open_meteo` blowing quota | 25 individual API calls | Batched 5 locations per call; 5 calls total |
| `open_meteo` 300s timeout in `run_all.py` | Underestimated rate-limit wait time | Raised to 1800s |
| OpenFDA `parse_exception` HTTP 500 | `+TO+` in query string double-encoded by requests | Use plain spaces; requests encodes them correctly |
| Treasury TIC wrong URL | Used `mfhhis.txt` → 404 | Correct URL: `mfhhis01.txt` |
| Treasury TIC SLT wrong approach | SHL survey URLs all 404 | Switched to `slt_table1.txt` (long-form monthly) |
| Treasury TIC parser failure | Used regex sep on tab-delimited file | Rewrote parser to split on `\t` |
| pytrends urllib3 error | `method_whitelist` renamed in urllib3 >= 2.0 | Removed `retries`/`backoff_factor` from `TrendReq()` |
| `UnicodeEncodeError` on Windows | `→` (U+2192) in print statements; terminal is cp1252 | Replaced with `->` ASCII |
| New tables `not in CATALOG` | Tables missing from `query.py` and `validate.py` | Added all 14 new tables to both |

---

## Repo State

- **GitHub**: up to date (`master`, latest commit: open_meteo batching rewrite)
- **C: drive** (`C:\Users\Zander\financial-data-pipeline`): working copy, up to date
- **D: drive** (`D:\Claude Main\Projects\financial-data-pipeline`): synced this session; needs one more pull after open_meteo run completes

D drive has some extra untracked local files (not in `.gitignore`):
```
alternative_data_sources.md   backfill_symbols.py    data_sources.csv
data_sources.md               patch_banks.py         validate_synthetic_options.py
Schwab API Exploration.ipynb  SchwabDev1.py          (etc.)
```
These are safe to ignore — they pre-date the current pipeline structure.
