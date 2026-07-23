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
| **PatentsView / FDIC failures** | Both hit DNS resolution failures (`getaddrinfo failed`) in the same run window — looks like a transient local network drop, not a dead source. Re-run before concluding either is broken. | 2026-07-23 stage-1 backfill live run |

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
previously gated the Schwab full price-history backfill — that backfill is now
blocked only on Schwab OAuth (interactive, see above), not on disk budget.

## In-flight initiative (started 2026-07-23)

Working through `EXPERT_BRIEF.md` roadmap items 1-4, then a full stage-1 backfill:

1. Daily automation (`ClaudeAuto-DailyAccumulators`) — **done**, see table above.
2. Schwab OAuth — **waiting on Zander** to run
   `schwab_quotes_pipeline.py` interactively (creates `tokens.db`, reused by all
   other Schwab pipelines).
3. Historical earnings — **AV path confirmed viable** (see constraints table);
   full backfill of `alpha_vantage_earnings`/`alpha_vantage_earnings_calendar` not
   yet run (quota-gated, needs pacing).
4. Factor evaluation pass — **done**. Only `momentum` clears significance
   positive (Sharpe 0.55 [0.23, 0.88]). `low_vol` clears significance
   *negative* (Sharpe -0.81 [-1.15, -0.51]) — the signal is inverted, not
   just noisy, and is dragging the equal-weighted composite down. Everything
   else (value, quality, growth, insider_flow, sentiment) is statistically
   indistinguishable from zero; `short_pressure` has too little date
   coverage (43 vs 121 registry rows) to judge yet. Proposed fix — zero (or
   sign-flip and re-test) `low_vol` in `analytics/signals.py`
   `DEFAULT_WEIGHTS`, down-weight the insignificant four — is written up but
   **not applied**, pending Zander's OK. Full table in
   `SESSION_NOTES_2026-07-19-eval-framework.md`.
5. Full `run_all.py --backfill --stage 1` — **running live, resumed after a
   timeout kill**. Dry-run first confirmed 63 pipelines queued, 6 skipped
   for missing env vars (expected: `trade`, `omkar_commodity`, `comtrade`,
   `reddit`, `ais`, `fed_sentiment`). Deliberately excluded throughout:
   `alpha_vantage_fundamentals` (shares the same 25/day AV quota as the
   earnings backfill — Zander chose to pace it separately) and
   `nasdaq_data_link` (confirmed dead end, see `CLAUDE.md`).
   **Note for future runs:** a `run_all.py --backfill --stage 1` sweep of
   this many pipelines does not fit in one 10-minute background command —
   the first attempt (task `bv1yomcmw`) was killed by the tool timeout
   partway through `fear_greed` (32 of ~61 active pipelines done). Resumed
   with `--skip <all completed pipelines>` as task `b25ng1ljy`, appending
   to the same log (`storage/quality_reports/stage1_backfill_2026-07-23.log`).
   Expect to need another `--skip`-based resume if it also hits 10 minutes;
   check task status before assuming a kill notification means failure.
   Clean completions confirmed so far: `commodity_macro`, `gas_prices`,
   `futures` (CFTC COT skipped — `cot_reports` package not installed),
   `short_interest`, `finnhub`, `finnhub_events`, `fundamentals`, `bls`,
   `treasury`, `world_bank`, `tiingo`, `institutional`, `noaa_climate`,
   `usda`, `eia`, `stockanalysis`, `finviz`, `coingecko`, `forex`, `bea`,
   `usgs_minerals`, `fama_french`, `shiller`, `cboe`, `ecb`. New breaks
   found (see constraints table): `oecd` (dead endpoint),
   `congressional_trades` (new 403 block); `patents`/`fdic` partially hit a
   likely-transient DNS blip. `dividends` got 0 rows (Finnhub 403) and
   `simfin` returned empty for every symbol despite a set API key — neither
   confirmed as a permanent break yet.
