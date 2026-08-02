# Session Notes — 2026-08-01

**Branch:** master
**Session model:** Claude Sonnet 5

## What happened

User asked "can we add any additional data to any of our pipelines or do anything to
improve them?" — this turned into a full NO-DATA-table audit (`status.py` showed 86 of
237 CATALOG tables with zero rows) plus the two stale backlog items from `TODO.md`.
Investigated every group, fixed what was fixable, documented what's genuinely blocked.

### 0. AV DJI earnings-pacing one-time retry — fired, still quota-blocked

`ClaudeAuto-AVEarningsPacing-20260801` fired at 10:05 AM as scheduled. Same result as
07-31: all 20 budgeted requests hit "standard API rate limit is 25 requests per day"
with zero real progress (still 9/30 DJI symbols). Confirms the parked rolling-24h
shared-IP-quota conflict with `earnings_sentiment_tool`'s 10:30 transcript pull is
still live and unresolved — not a new bug. Flag cleared after review. The task
self-deleted per its one-time design; not re-registered (see PROJECT_NOTES for the
4 open options, still undecided).

### 1. TODO.md backlog items — already resolved by a prior session, just undocumented

- **9 broken FRED series (commodity_macro_pipeline.py)**: already replaced with valid
  series IDs (`PCU3259103259101`, `PCU331110331110`, etc.) in the current source, with
  two explicitly marked permanently-discontinued (Gold/Palladium/Platinum IBA data,
  deleted from FRED Jan 2022; Glass Mfg + Specialty Glass PPI, discontinued by BLS July
  2025). Verified all 7 live series still resolve today.
- **OECD pipeline (SDMX-JSON decommission)**: already rewritten 2026-07-29 against the
  new `sdmx.oecd.org` REST API. Verified live: 5,246 rows, 8 indicators, 14 countries.

### 2. Root-cause bug: pandas 3.0.3 broke `pd.read_html()` on pre-fetched HTML strings

The single highest-leverage find. `pandas` is now 3.0.3 in this environment;
`pd.read_html(html_string, ...)` no longer accepts a raw string — it tries to open it
as a filename/URL, throws `OSError`, which both `finviz_pipeline.py` and
`stockanalysis_pipeline.py` were silently swallowing as "no data" via a bare
`except Exception: return []`. Fix: wrap in `io.StringIO(html)` at both call sites.
This alone explained all 8 `finviz_*` and all 11 `sa_*` NO-DATA tables.

Two more real bugs surfaced once the crash was fixed and real HTML started flowing:

- **`finviz_pipeline.py`**: `_find_table_with_col` picked the *first* table matching a
  `Ticker` column, but Finviz's page now has several small decoy widgets (recent-tickers
  strip, etc.) ahead of the real results grid in document order. Changed to pick the
  *largest* matching table. Screener 2 -> 500 rows; movers 16 -> 1,098 rows.
- **`stockanalysis_pipeline.py::run_financials`**: (a) `_normalize_cols` collapsed two
  originally-distinct raw date-column headers into an identical string for some
  symbol/period combos, producing duplicate column labels pyarrow rejects at write time
  — fixed by deduping columns both per-symbol (before concat) and again after
  normalization (belt-and-suspenders, the second one is what actually mattered). (b)
  `run_financials` never called the existing `_coerce_numerics` helper (movers/screener
  do), so mixed str/float columns crashed the Arrow conversion — added the call.
  `sa_income`/`sa_balance`/`sa_cashflow`/`sa_ratios` now populated.

### 3. `dark_pool_pipeline.py` — full rewrite, source endpoint retired

`otctransparency.finra.org`'s REST path now returns the site's Angular SPA shell
(HTTP 200, `text/html`) for every request instead of JSON — FINRA retired it. Found the
real replacement: `api.finra.org/data/group/otcMarket/name/weeklySummary` (same public
data-group gateway, POST-based, keyless, paginated at 5,000 rows/request via
`limit`/`offset`). Rewrote the pipeline against it. Verified live: 72,318 rows (one
month). No clean natural dedup key exists for this dataset (~12% residual duplicates
even on the widest reasonable key — likely revised/re-reported weeks with no
distinguishing revision column) — `curated.py` intentionally left on safe full-row
dedup rather than risk a wrong key.

### 4. `eia_expansion_pipeline.py` — 5 distinct EIA v2 API parameter bugs, all fixed

EIA's v2 API uses inconsistent facet/data-field naming per dataset (undocumented
inconsistency, confirmed empirically against `EIA_API_KEY` for each). Fixed all 7
sub-datasets:

- `electricity_generation`: facet `sectorId`->`sectorid`, `stateid`->`location`;
  data field `fuel2002`->`fueltypeid`; response field `generation` (not `value`).
- `electricity_sales`: response is wide (`sales`/`revenue`/`price` as separate
  columns, not one `value` column) — `sales` now maps to `value`, `revenue`/`price`
  kept alongside.
- `nuclear_outages`: data fields `outage-mwg`/`percent` don't exist, renamed to
  `outage`/`percentOutage` by EIA; response also wide (`capacity`/`outage`/
  `percentOutage`) — `outage` (MW offline) maps to `value`.
- `coal_production`: facet `coalRank`->`coalRankId`; **and** the rank codes
  themselves were wrong (`an`/`bi`/`sb`/`li` don't exist — real values are
  `ANT`/`BIT`/`SUB`/`LIG`, confirmed via EIA's facet-metadata endpoint).
- `coal_trade`: response wide (`quantity`/`price`, not `value`); also no
  destination/origin pair, just one `countryId` + an `exportImportType` flag.
- `seds`: real facets are only `seriesId`/`stateId` — the `fuelId`/`sectorId` facets
  in the old code don't exist on this dataset at all. Rewrote to fetch by `stateId`
  only; `fuel_code`/`sector_code` are now a best-effort raw prefix split of the 5-char
  MSN `seriesId` (not an authoritative decode — EIA's full MSN reference table isn't
  reproduced here; `series_description` carries the real human-readable label).
- (coal_trade's data-field bug was already latent-fixed by the same wide-format
  pattern found in nuclear/sales.)

All 7 verified live: 32 + 580 + 179 + 209 + 1,965 + 195 + 48,035 rows respectively.
`validate.py`/`curated.py` schemas updated to match the corrected column sets.

### 5. `open_meteo_pipeline.py` — not a bug, just never run in the mode that works

07-23's failure was `--backfill` (35 years x 5 locations x 11 vars per request)
exceeding Open-Meteo's per-request response-size cap — a real API constraint, not a
parameter bug. Incremental mode (2-year window) was never retried afterward. Ran it:
18,275 rows, clean. (`--backfill` still needs date-chunking to get real historical
depth — not done this session, low priority since incremental now accumulates daily.)

### 6. Confirmed external, unfixable (vendor tightened free-tier access)

- **Finnhub**: `dividend_pipeline.py` (`/stock/dividend2`, all 30 symbols),
  `finnhub_expansion_pipeline.py` (10 of 12 endpoints: esg, congressional-trading,
  supply-chain, social-sentiment, earnings-quality-score, lobbying, usa-spending,
  uspto-patents, visa-applications, economic-calendar), and
  `finnhub_fundamentals_pipeline.py` (`/stock/transcripts/list`, likely siblings) all
  403 `"You don't have access to this resource."` — a free-tier gating change on
  Finnhub's side sometime before today, not present when these pipelines were built.
  Only `insider_sentiment`/`sec_filings` from the expansion pipeline still work.
- **Tiingo**: `/tiingo/corporate-actions/<symbol>/distributions` and `/splits` 403
  "lacks add-on entitlement" for every symbol — that add-on isn't on the free/Power
  plan. The sibling `/distributions-yield` endpoint has no such gate and still works
  (1,922 rows, `tiingo_corporate_actions_yield`).
- **Congressional trades**: both House and Senate disclosure-aggregator endpoints
  still 403 (unchanged from 07-23) — looks like anti-bot hardening, not a URL bug.
- **BLS** (`bls_oes_qcew_pipeline.py`, `bls_expansion_pipeline.py`): hit the daily
  registration-key quota (already spent earlier today) — not a bug, just needs a
  retry on a day with headroom.

### 7. Found but not yet implemented: real fix for `finra_short_interest`

Same discovery pattern as item 3 — `api.finra.org/data/group/otcMarket/name/
equityShortInterest` is public/keyless and returns real biweekly Reg SHO short-interest
records (confirmed live), replacing the CDN path documented as dead in `CLAUDE.md`.
Looks OTC-market-scoped rather than full NMS coverage (`equityMarket` group needs a
registered credential, 401 keyless) — needs one more verification pass before
rewriting `short_interest_pipeline.py`'s `--source finra` path. Logged as a follow-up,
deprioritized this session.

### 8. Other quick wins run clean

`insider_sentiment_pipeline.py` (141 rows), `indeed_hiringlab_pipeline.py` (319,410
rows — previously implementation-verified 07-28 but apparently never actually
persisted/kept in this store).

## Net result

Fixed or newly populated this session: `finviz_movers`, `finviz_screener`,
`finviz_financials`, `finviz_insider`, `finviz_sector_perf`, `finviz_industry_perf`,
`finviz_group_valuation` (partial — country_perf still empty, low priority), `sa_income`,
`sa_balance`, `sa_cashflow`, `sa_ratios`, `sa_stock_list`, `sa_etf_list`, `sa_movers`,
`sa_ipos`, `sa_ipo_calendar`, `sa_corporate_actions`, `dark_pool_volume`,
`eia_electricity_generation`, `eia_electricity_sales`, `eia_nuclear_outages`,
`eia_coal_production`, `eia_coal_trade`, `eia_seds`, `open_meteo_weather`,
`insider_sentiment`, `indeed_job_postings_national/sector/state`,
`tiingo_corporate_actions_yield`, `fred_rates_gdp_labor` (ran clean via full spec).
That's roughly 30 of the original 86 NO-DATA tables. Remaining NO-DATA tables are
either genuinely blocked on a missing user API key/signup (Reddit, AISStream,
Comtrade, Census, fresh USDA_NASS key, USPTO ODP key, Schwab Trader API enablement)
or on a vendor-side dead end documented above.

## Next up

- Full Schwab price-history backfill (`price_history_pipeline.py --full`), sized
  first — in progress as of this writing.
- `finra_short_interest` rewrite via the `equityShortInterest` FINRA endpoint found
  above (item 7).
- `open_meteo_pipeline.py --backfill` date-chunking for real historical depth.
- Retry `bls_oes_qcew_pipeline.py`/`bls_expansion_pipeline.py` on a day with fresh
  BLS quota.
- Still parked: AV DJI earnings-pacing 4-option decision (unchanged from 07-31).
