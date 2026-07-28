# Session Notes — running log

## 2026-07-28 — Reconciled a month-long diverged fork into origin/master

A separate local checkout had fallen behind `origin/master` by 116 commits after a `git fetch`
surfaced the divergence (common ancestor `4c5ee86`, 2026-06-29). Rather than merge blind, worked
through it incrementally on a new `reconcile-onto-origin` branch (based on `origin/master`, not
touching `master` or `origin/master` directly):

- **Chose `origin/master` as the merge base** — it had ~30 more pipelines, a `logging_utils.py`
  framework already wired into the runner, `curated.py` compaction, and interactive-OAuth handling
  the fork lacked. The fork's own `pipeline_logging/` package (built 2026-07-19, migrated across
  48 pipelines) and its `catalog.py` extraction are **not** being carried forward — superseded by
  origin's simpler, already-established convention (pipelines use `print()`; `CATALOG` stays
  inline in `query.py`).
- **Verified rather than blindly ported the fork's "9 new pipelines" batch** — found 6 of 9
  (`cds_spreads`, `freight_rates`, `housing`, `leveraged_loans`, `muni_bonds`, `semiconductor`)
  were either duplicates of tables `origin/master` already pulls (e.g. `commodity_macro_pipeline.py`
  already has the real `BAMLH0A0HYM2`/`BAMLC0A0CM` credit-spread series) or built on FRED series IDs
  that don't match any real series (`BAMLC0A5YSP`, `BAMLH0A1HY0`, `LEVLPREI`, `MUNIYLD`, `ISMT`,
  etc. — verified via web search; real ICE BofA series follow patterns like `BAMLH0A0HYM2` /
  `BAMLH0A1HYBB`). `freight_rates_pipeline.py` also claimed FRED carries "Baltic Dry Index" data;
  origin's own `shipping_pipeline.py` docstring explains why that's wrong (paid-license-only, hence
  the FRED PPI-proxy approach it uses instead). Ported only the 3 that checked out —
  `dark_pool_pipeline.py` (FINRA OTC Transparency), `retail_sentiment_pipeline.py` (Stocktwits),
  `insider_sentiment_pipeline.py` (SEC EDGAR Form 4) — wired into `query.py` CATALOG, `validate.py`,
  `run_all.py`, `curated.py`, and both catalog/pipeline test files. Added the 2 genuinely-new
  housing series (`ASPUS`, `MSACSR`) directly into `fred_macro_pipeline.py` instead of standing up
  a duplicate `housing_pipeline.py`.
- **Same scrutiny caught a broken analytics suite** — all 11 modules in the fork's
  `analytics/` (chokepoint_volatility, correlation_regime, geopolitical_risk, etc.) import
  `get_connection` from `query.py`, which doesn't exist on either branch — a hard `ImportError`
  that means this suite was never actually run. `chokepoint_volatility.py` also hand-builds a
  `storage/raw/market/` glob path that doesn't match any real table. **Deferred, not ported** —
  needs real per-module rework, not a merge. Tracked as a follow-up in
  `FinancialDataPipeline_Future_Improvements.md`.
- **Ported the 5 tooling scripts** — `cost_tracker.py`, `quota_tracker.py`, `storage_monitor.py` as-is
  (no query/catalog dependency); `anomaly_detector.py` and `freshness_dashboard.py` needed one fix
  each (`import catalog as c` → `import query as c`, since only `c.CATALOG` was used).
- **Ported `ADVERSARIAL_REVIEW.md`** (22 findings from a 2026-07-27 review of the fork) with a
  preamble noting which findings were spot-checked against current `origin/master` — 2 of 5
  CRITICAL findings were already independently fixed there, 2 are still open, 1 unconfirmed. Its
  "Missing Data Sources" table is the origin of the 9-pipeline batch above; treat any remaining
  unbuilt item there with the same skepticism.
- Full test suite on the reconcile branch: **467 passed, 4 skipped**, one pre-existing failure
  (`test_storage_dirs_exist`, unrelated — storage dirs for origin's own newer pipelines that have
  simply never been run in this fresh checkout).

**Next step:** review `reconcile-onto-origin` and merge into `master` once satisfied; the deferred
analytics suite and any remaining unverified "Missing Data Sources" ideas are follow-up work, not
blockers.

### Landed: master reset to the reconciled branch, pushed, forks cleaned up

A literal `git merge` of `reconcile-onto-origin` into `master` would have recreated every conflict
above from scratch (their only common ancestor is `4c5ee86`, so git would 3-way merge master's 11
orphaned commits against all 117 origin+reconcile commits). Instead: `git reset --hard
reconcile-onto-origin` while on `master` — safe since nothing on the old `master` was unique or
lost (its useful parts were already ported above; the rest was deliberately dropped). Verified
clean (467 passed / 4 skipped / 1 unrelated pre-existing failure) then `git push origin master` —
a plain fast-forward (`38caf4c..a377fe2`), no force needed. `master` and `origin/master` are now
identical.

Before deleting `local-catchup` (the pushed copy of the old fork), audited its full diff against
the new `master` for anything not yet accounted for. Specifically checked for a backtesting-engine
plan built earlier with Fable — found it's the "Unified Evaluation Framework"
(`docs/superpowers/specs/2026-07-18-unified-eval-framework-design.md`, commits `dd23e12`/`5bd7815`,
both `Co-Authored-By: Claude Fable 5`) plus its implementation (`evaluate.py`, `evaluation/`
package, `backtest.py`, `event_backtest.py`) — all already ancestors of `master` via
`origin/master`'s history, never part of the stale fork at all (the diff direction looked
backwards at first glance: those files show as "deleted" going from `master` to `local-catchup`
because `local-catchup` predates them, not because it holds a copy). Confirmed nothing else on
`local-catchup` was unique beyond what's already listed above (the 6 dropped pipelines, the
deferred analytics suite, the superseded logging/catalog infra). Deleted `local-catchup` (remote +
local) and `reconcile-onto-origin` (local-only, never pushed). Only `master` remains, clean and in
sync with `origin/master`.

---

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
