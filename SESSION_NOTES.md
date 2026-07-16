# Session Notes — running log

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
