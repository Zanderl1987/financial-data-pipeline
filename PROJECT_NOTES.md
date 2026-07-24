# Project Notes — financial-data-pipeline

A living reference of current project state: what's automated, what's hard-blocked
and why, and what depends on things outside this repo. Unlike the dated
`SESSION_NOTES_*.md` files (chronological, append-only) and `EXPERT_BRIEF.md`
(prioritized roadmap with reasoning), **this file is updated in place** — stale
entries get corrected or removed, not appended around. If a fact here looks wrong,
trust the code/live state over this file and fix the entry.

## Active automations

| Task | Schedule | What | Failure flag |
|---|---|---|---|
| `ClaudeAuto-PipelineQuality` | Mon 9:30 AM | `validate.py` full health check | `QUALITY_FAIL.txt` |
| `ClaudeAuto-DailyAccumulators` | Daily 9:00 AM | `run_all.py --only tradingview,short_interest,finnhub_events` | `DAILY_ACCUMULATOR_FAIL.txt` |

Both catch up after boot if the machine was off (`StartWhenAvailable`). See
`AUTOMATION.md` for full detail and management commands. Check both flag files at
the start of any session that touches pipelines or analytics.

## Known hard constraints (verified live, not assumed)

| Source | Constraint | Verified |
|---|---|---|
| **Alpha Vantage** | 25 req/day/key, **shared per-IP across repos** — this repo's `alpha_vantage*` pipelines compete for the same daily quota as `custom_index_tool`'s TranscriptPull (~10:30) and EarningsSurprisePull. Spend non-transcript AV quota in the ~10:05 gap when doing cross-repo work. | ongoing |
| **Finnhub earnings** (`/calendar/earnings`, `/stock/earnings`) | Free tier will not return data older than ~1 year, even when explicitly requesting a 365-day backfill window (`from`/`to` params are silently clamped/ignored for the far past). This is an API ceiling, not a pipeline bug — do not re-attempt by widening date-range args. | 2026-07-18 backfill run + 2026-07-23 re-check |
| **Alpha Vantage EARNINGS** (`fetch_earnings()` in `alpha_vantage_fundamentals_pipeline.py`) | Returns full history back to ~1996 per symbol (121 quarterly + 31 annual rows for AAPL/MSFT). This is the real fix for the earnings-history gap Finnhub can't solve — `alpha_vantage_earnings`/`alpha_vantage_earnings_calendar` tables are wired but currently empty; populating them is quota-gated (25/day shared, see above) so budget several days for the watchlist. | 2026-07-23, 2-symbol probe |
| **SEC EDGAR** | ≤10 req/s hard limit (8 req/s targeted safe); violating it triggers a ~10 min IP block. Mandatory `EDGAR_USER_AGENT` header. | code-level, longstanding |
| **openFDA** | 1,000 req/day keyless, 120,000/day with a free key. | code-level |
| **Comtrade** | ~60 req/hr keyless (recent years only); 500 req/day with key (full history to 1988). | code-level |
| **Omkar commodity** | 100 req/**month** — the only monthly-capped source in the registry. | code-level |
| **Tiingo** | ~50 symbols/hour on the free tier (soft, not blocking). | code-level |
| **Schwab** | OAuth is interactive, code expires ~30s — cannot be automated or run unattended. Trader API (positions/transactions) needs separate enablement at developer.schwab.com beyond the base Market Data API. | longstanding |
| **Finnhub free tier** | `stock/price-target`, `stock/upgrade-downgrade`, and `stock/dividend2` all 403 ("You don't have access to this resource") for every symbol — a permissions gap on the free plan, not a pipeline bug. `stock/insider-transactions` is capped at 100 rows/request regardless of date range (pipeline already logs a truncation NOTE per symbol; some DJI names have 1000+ transactions in the requested window). | 2026-07-23 stage-1 backfill live run |
| **FRED series IDs** | 9 of the 64 requested series 400 ("series does not exist"): `GOLDPMGBD228NLBM`, `PPALAUSDM`, `PPLATINUMUSDM`, `WPU1019A2S`, `PCU3311103311101`, `PCU3272133272131`, `PCU3272143272141`, `WPU0619`, `PCU3272153272151` — likely discontinued/renamed by FRED; needs replacement IDs found, not a pipeline bug. | 2026-07-23 stage-1 backfill live run |
| **OECD MEI (`stats.oecd.org/SDMX-JSON`)** | Every one of the 8 requested series 404s. Looks like OECD retired/moved this endpoint (they've been migrating to a new Data Explorer API) — a likely permanent break needing a pipeline rewrite against the new API, not a transient issue. | 2026-07-23 stage-1 backfill live run |
| **Congressional trade disclosures** | Senate + House disclosure sites both 403 on all 3 retry attempts — looks like a new bot-detection block on a previously-keyless source. | 2026-07-23 stage-1 backfill live run |
| **FDIC** | The stage-1 DNS resolution failure was transient — re-run 2026-07-23 pulled clean (4,255 institutions, 98,669 financials, 4,115 failures). Not a dead source. | 2026-07-23, re-run confirmed fixed |
| **PatentsView (`patents_pipeline.py`)** | NOT a transient DNS blip — `search.patentsview.org` returns NXDOMAIN (confirmed via `nslookup`; other hostnames resolve fine). Root cause: PatentsView migrated to the USPTO Open Data Portal (`data.uspto.gov`) around March 2026; `patentsview.org` now 301-redirects there, and the old API's original endpoints reportedly return 410 Gone. This is a real breaking change needing a pipeline rewrite against the new ODP API (new auth requirements, `size`/`after` paging instead of `per_page`/`page`), not a re-run. Not yet rewritten. | 2026-07-23, confirmed via nslookup + web research |
| **Fed SOMA backfill** | Times out at its internal 3600s limit before completing — this source needs a dedicated run with a longer timeout, not a re-run inside the full stage-1 sweep. The killed run also left a truncated (unreadable) partial parquet file behind; a timed-out pipeline can do this, so check for "No magic bytes found" errors after any timeout kill. | 2026-07-23 stage-1 backfill live run |
| **`google_trends_pipeline.py`** | Was `ModuleNotFoundError: No module named 'pytrends'` — the package was listed in `requirements.txt` (unpinned) but never actually installed. Fixed 2026-07-23: `pip install pytrends` (4.9.2). Verified live run — 3 keyword groups, 1,365 rows each, compacts clean. No repo file changed (env-only fix). | 2026-07-23, fixed and verified |
| **Hive partition mismatches (raw store)** | Several raw dirs (`futures`, `sec_edgar/submissions`, `sec_edgar/xbrl_fundamentals`, `cot`, `synthetic_options`, `options_history`) had legacy files sitting directly in the table dir instead of under `year=/month=`, predating `write_partitioned()`'s adoption for those pipelines. `read_parquet(..., hive_partitioning=true)` throws a Binder Error the moment a partitioned sibling file appears alongside one, breaking `curated.py` compaction for the whole run (not just the offending table). Fixed 2026-07-23 by moving all 7 stray files into their correct `year=/month=` dir (partition inferred from each file's `fetched_at`/`fetch_date`). If a new stray unpartitioned file ever appears, find it via: `glob.glob('storage/raw/**/*.parquet', recursive=True)` filtered to paths without `year=`. | 2026-07-23 |
| **Iceberg CATALOG path bug (fixed)** | `query.py`'s `CATALOG` entries for `index_members`, `securities`, `fund_holdings`, `identifier_map`, `shipping_gscpi`, `shipping_freight_ppi` used `_glob()`, which always roots at `storage/raw/`, but the real Iceberg data lives at `storage/iceberg/...`. Since `_register_views()` silently skips a table when its glob matches zero files, these 6 views (and `index_holdings`, the composite view built on 3 of them) simply didn't exist — no error, just silently missing data. Leftover from the `d5dd859` Iceberg migration (2026-07-22). Fixed 2026-07-23: added `_iceberg_glob()` rooted correctly. These tables were also never wired into `curated.py`'s `KEYS` (a second gap from the same migration) — fixed too, since the path fix alone would have surfaced raw duplicates (49.7% for `securities`, 50% for both shipping tables). `_sort_recency` now also recognizes `last_refreshed` (securities' timestamp column). Verified: `curated.py` compacts 139 tables (was 133), `index_holdings` returns 15,199 rows. Committed `8e7b05f`. | 2026-07-23, fixed and verified |

## Cross-repo dependencies

- **`custom_index_tool`** (earnings-call verbosity study) shares this machine's IP
  for Alpha Vantage quota (see above) — coordinate before running AV-heavy backfills
  here. It also wants an independent "bad news" label per (ticker, quarter), which
  this repo can supply via `earnings_surprise` analytics + `event_backtest.
  earnings_events()` once earnings history is deep enough (see AV earnings finding
  above). See `custom_index_tool/EXPERT_BRIEF.md`.

## Storage

As of 2026-07-22 the curated snapshot is published publicly at
`https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline` (public HF
dataset repos get effectively unlimited storage, ~1TB soft cap raised on request;
private repos cap at 100GB free). This removes the storage constraint that
previously gated the Schwab full price-history backfill. Schwab OAuth is now
also done (2026-07-24, see In-flight initiative below) — that backfill is
fully unblocked, just not yet started.

**Iceberg snapshot growth (watch this):** Iceberg keeps every historical
metadata/manifest file with no expiration configured. The 2026-07-23 stage-1
backfill alone generated 149 new snapshot files for `fund_holdings` (vs ~30
files total across all Iceberg tables at initial migration) — looks like that
pipeline writes a new snapshot per symbol/batch rather than one per run. Not
urgent yet (1.7MB added), but will compound under `ClaudeAuto-DailyAccumulators`
if daily runs touch it. Follow-up: batch `fund_holdings` writes and/or add
Iceberg snapshot expiration before this grows unbounded in git history.

## In-flight initiative (started 2026-07-23)

Working through `EXPERT_BRIEF.md` roadmap items 1-4, then a full stage-1 backfill:

1. Daily automation (`ClaudeAuto-DailyAccumulators`) — **done**, see table above.
2. Schwab OAuth — **done, 2026-07-24.** Old `tokens.db` refresh token (issued
   2026-07-04) had passed Schwab's 7-day expiry. Completed the OAuth handshake
   non-interactively: opened the auth URL, Zander authorized in-browser, then
   fed the resulting (failed-to-load) `127.0.0.1:8182` redirect URL into
   `schwabdev.Client(..., call_on_auth=lambda url: redirect_url,
   open_browser_for_auth=False)` instead of the library's normal blocking
   `input()` prompt. First attempt's code expired (~30s window) while
   confirming the correct kwarg name; second attempt succeeded. Fresh
   `tokens.db` verified live via `schwab_quotes_pipeline.py` (45 symbols,
   real quote/PE data). Found and fixed a Windows cp1252 crash (unicode arrow
   in a print statement, after data was already saved) in all 5 Schwab
   pipelines — never surfaced before since none were runnable without valid
   tokens. Committed (pipeline fixes only; `tokens.db`/`.env` gitignored).
3. Historical earnings — **pacing started 2026-07-24.** Ran
   `alpha_vantage_fundamentals_pipeline.py` (incremental mode, default
   20-request budget) late evening 2026-07-23 — the ~10:05-10:30am "safe
   window" assumption in the constraints table turned out not to be a hard
   requirement; a live probe request succeeded well outside that window,
   confirming the day's shared AV quota wasn't already exhausted by
   `custom_index_tool`'s automations. First batch: earnings history for
   GOOGL/AXP/AMGN/AMZN/AAPL/BA/CAT (1,017 records), full earnings calendar
   (4,885 upcoming dates, all tickers), plus overview (7 symbols), dividends
   (2 symbols), insider transactions (2 symbols) — all from the pipeline's
   built-in rotating-subset mechanism (`INCREMENTAL_EARNINGS_N=7`/day), which
   will cover the full 30-symbol DJI universe over ~5 more daily runs. Not
   wired into any scheduled automation — `scripts/daily_accumulators.ps1`
   deliberately excludes it (AV pipelines are quota-sensitive, run manually/
   paced by hand, not on a fixed schedule). `curated.py` verified: 148 tables
   (up from 143), `alpha_vantage_earnings`/`alpha_vantage_earnings_calendar`
   both queryable with real data. One unrelated bug surfaced: `news_sentiment`
   section failed ("Invalid inputs", AV API) and burned 1 budget slot for
   nothing — not investigated, since the sentiment factor already uses local
   VADER (see `CLAUDE.md`), not this endpoint. Continue pacing on subsequent
   days to complete the DJI universe.
4. Factor evaluation pass — **done, and applied.** Only `momentum` cleared
   significance positive (Sharpe 0.55 [0.23, 0.88]). `low_vol` cleared
   significance *negative* (Sharpe -0.81 [-1.15, -0.51]) — confirmed a real,
   regime-invariant inversion (negative IC in every regime slice — bull,
   bear, high-vol, low-vol — plus walk-forward OOS), not a look-ahead
   artifact or single-period fluke. Zander approved: sign-flipped `low_vol`
   in `analytics/signals.py` (now longs volatility instead of calm) and
   zeroed `value`/`quality`/`sentiment`/`insider_flow` in `DEFAULT_WEIGHTS`
   (`growth`/`short_pressure` left at 1.0 — borderline / insufficient
   coverage, not clearly null). Composite Sharpe after the change: -0.11
   [-0.45, 0.21] -> 1.01 [0.70, 1.32]. Committed `3b363af`. Full eval table
   in `SESSION_NOTES_2026-07-19-eval-framework.md`.
5. Full `run_all.py --backfill --stage 1` — **done.** 27 PASS, 2 FAIL
   (`fed_soma` timed out at its internal 3600s limit — needs a dedicated
   longer-timeout run, not a re-run inside the sweep; `google_trends` —
   missing `pytrends` dependency, one-line fix not yet applied), 6 SKIP (all
   expected missing-env-var skips: `trade`, `omkar_commodity`, `comtrade`,
   `reddit`, `ais`, `fed_sentiment`). 117m12s total. Deliberately excluded
   throughout: `alpha_vantage_fundamentals` (AV quota, paced separately) and
   `nasdaq_data_link` (confirmed dead end). Needed two resumes to get past
   the tool's 10-minute background-command timeout (`bv1yomcmw` then
   `b25ng1ljy`, same log, `storage/quality_reports/stage1_backfill_2026-07-23.log`).
   New source breaks found: `oecd` (dead endpoint), `congressional_trades`
   (new 403 block); `patents`/`fdic` DNS blips look transient, re-run before
   concluding broken; `dividends` (Finnhub 403) and `simfin` (empty despite
   a set key) not yet confirmed as permanent breaks.

   Post-backfill, `curated.py` compaction failed on a Hive partition mismatch
   in `futures` — traced to a legacy unpartitioned file, which led to finding
   and fixing 6 more of the same class across the raw store, plus a
   timeout-corrupted `fed_soma` parquet file blocking compaction entirely.
   Also surfaced and fixed the Iceberg CATALOG path bug (see constraints
   table for both) via `tests/test_catalog.py::test_storage_dirs_exist`, and
   the `google_trends` missing-dependency fix. All three follow-up fixes are
   now done and verified — `curated.py` compacts 142 tables clean.

**Initiative status:** items 1, 2, 4, and 5 (plus all follow-up fixes
surfaced by 5, and the FDIC/PatentsView re-check) are complete. Item 3 (AV
earnings backfill) is in progress — first paced batch ran 2026-07-24, ~5
more daily runs needed to cover the full DJI universe. All 5 original
roadmap items have now been started.

**Schwab full price-history backfill — done, 2026-07-24.** Ran
`price_history_pipeline.py --full --watchlist` (the standard 63-symbol
watchlist from `tiingo_pipeline.DEFAULT_SYMBOLS` — DJI 30 + high-interest
tech + broad market/sector/bond/commodity ETFs — not just the pipeline's
DJI-30 default, since that's the universe `event_backtest`/`signals`
actually run against). Fixed the same Windows cp1252 unicode-arrow crash
found in the other 5 Schwab pipelines before running. 62/63 symbols
succeeded, 473,916 rows, daily bars back to 1984-11-01 for the oldest DJI
names (AXP, BA, MMM) and to each ETF/stock's actual listing date otherwise
(e.g. PLTR from its 2020-09-30 IPO). `WBA` returned `{"empty":true}` from
Schwab itself (HTTP 200, not an error) — likely delisted/inactive on their
end, not a pipeline bug; not investigated further. `curated.py` verified:
`prices` table now 484,371 rows, spans 1984-2026. This was the last item
deferred pending Schwab OAuth (see Storage section) — fully done now.

## Full-universe expansion (started 2026-07-24)

Zander's direction: grab as much data as possible for the *entire* symbol
universe Schwab can see (not just the 63-symbol watchlist), using
Schwab specifically because it has no daily quota (120 req/min hard cap
only, vs. AV's 25/day) — quota-gated sources (AV, etc.) stay scoped to the
watchlist/DJI-30 and get used "selectively" to fill in deeper data as
specific projects need it. This is a baseline-breadth-first, depth-later
strategy, not a one-time task.

**Universe discovery:** Schwab's `instruments()` endpoint has no bulk-dump
mode — it's regex-search only, minimum 2 literal characters, and results
mix in mutual funds/options/notes alongside real equities. Swept all
26x26 two-letter prefixes (676 calls) plus the 26 true single-letter
tickers (exact match), filtered to `assetType` in {EQUITY, ETF}. Result:
**29,374 unique symbols** (22,984 equities + 6,390 ETFs) across all
exchanges Schwab tracks — 13,219 on major exchanges (NASDAQ 5,661 / NYSE
2,975 / NYSE Arca 2,704 / Cboe BZX 1,543 / NYSE American 325 / MEMX 11)
and 16,155 OTC/pink-sheet (OTC Markets 15,328 / Nasdaq OTCBB 827). Zander
chose to include everything, reasoning the HuggingFace publish target has
effectively no storage ceiling for this scale (~1TB soft cap; see Storage
section — this pull is estimated at single-digit GB, nowhere close).
Saved as `symbol_universe.csv` (repo root, git-tracked — this is now the
reference definition of "our universe," not raw fetched data).

**Backfill mechanics:** `schwab_universe_backfill.py` (new script) reuses
`price_history_pipeline.fetch_symbol()` (same fetch/backoff/derived-columns
logic) but can't reuse `main()`'s single-shot design — pulling full history
for 29,374 symbols would produce 100M+ rows, too large to hold in memory or
write in one parquet file. Instead: chunks of 250 symbols, writing (and
checkpointing progress to `schwab_universe_backfill_progress.json`,
gitignored — regenerable state, not data) after every chunk, so a
multi-hour run survives interruption and can resume without re-fetching
already-completed symbols. Smoke-tested on 8 real symbols (7/8 got data,
1 empty) and verified the resume path skips already-handled symbols before
launching the real run. Estimated total runtime ~4.5 hours at the 0.55s/
request pacing already used elsewhere in this codebase.

**Status: done, 2026-07-24.** Run crashed once at 63.8% on a transient DNS
blip for `api.schwabapi.com` (`fetch_with_backoff` only retries HTTP error
codes, not raw connection exceptions) — hardened `schwab_universe_backfill.py`
with a per-symbol retry (3 attempts, 15/30/45s backoff) and a `failed`
progress bucket distinct from `empty`, resumed cleanly from the last
checkpoint (no work lost). Final result: **27,759 symbols with real price
data, 1,616 empty (confirmed no data at Schwab), 0 failed**, out of 29,374.

Folding into `curated.py` then hit a second, more fundamental problem:
`compact()`'s normal path (`q.load()` into pandas, then `sort_values` +
`drop_duplicates`) OOM-killed twice trying to materialize the resulting
47.5M-row `prices` table — confirmed via isolated testing that it dies
mid-load, before dedup logic even runs (machine has 32GB RAM, ~10.6GB free
at the time). Fixed in `curated.py`: added `_compact_large_table()` for a
new `_LARGE_TABLES` set (`prices` only, for now) — same keyed dedup logic
(`ROW_NUMBER() PARTITION BY (symbol, date) ORDER BY fetched_at DESC`) but
done entirely inside DuckDB via `COPY`, which streams/spills instead of
holding everything as a pandas object-dtype DataFrame. Wired into both
`compact()` and `compact_all()` as a name-based branch; the other 147
tables are untouched, still on the original pandas path. Compacts `prices`
in ~19 seconds now (47,466,193 -> 46,950,543 rows, 515,650 dupes removed)
instead of crashing. Full test suite: 11/11 in `test_curated.py` pass,
including the parametrized real-data duplicate-key check for `prices`.

Also noticed benign `RuntimeWarning: divide by zero/invalid value in log`
noise in the backfill log — `compute_derived_columns`' `log_return` hitting
zero-price days on some penny/OTC names now in scope. Doesn't crash
anything, just leaves `inf`/`NaN` in that column for those specific rows —
a data-quality note for whenever this data actually gets used, not fixed
now.

`prices` table is now 46,950,543 rows across 27,759 symbols (1985-2026),
verified queryable via `query.py`. Not yet re-synced to HuggingFace — that
publish is manual (`upload_huggingface.py`) and this is now a much bigger
jump in size than the 270MB→~300MB deltas so far; worth doing deliberately
rather than as an afterthought given the scale change.
