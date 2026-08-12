# Session Notes — running log

## 2026-08-12 — TV strategy catalog: Batch 1 collection (4 more scripts)

Detail in `SESSION_NOTES_2026-08-12_tv-catalog.md`; summary here.

Collected and Stage-1 screened 4 more community strategies from the amended priority-1
frame (Strategies + Open-source only + Most popular): `rabiah6x_ut_bot_scalper`
(**excluded**, `unconfirmed_htf`), `supertrend_entry_tp123`, `vegas_channel_tunnel_v11`,
`hybrid_breakout_vcp`. Directory is now 6 `.pine` files, 5 admitted to Stage 2. Still
Stage 0/1 only — nothing translated, no endpoint computed.

Saved the full 23-entry sampling roster with per-slug status to
`storage/tv_scripts/_roster_strategies_popular_2026-08-12.txt` (13 still TODO), so
future sessions resume from a file instead of re-enumerating the site.

New pre-registration amendment (2026-08-12): scripts over ~300 lines are excluded
`SKIP-LEN`, because Pine source can only leave a script page through the two-channel
workaround and both channels pass through the collecting session's context. Two possible
escape hatches were tested and are closed: `btoa()` returns are rejected outright by the
content filter, and `requests.get()` on a script page returns ~518 KB of HTML with no
`@version` anywhere (client-rendered). Also corrects an 08-11 note: the source block is
not a live Monaco editor (0 `.view-line` nodes), so it is not virtualized and the whole
file is in the DOM at once.

Two open decisions, both now in TASKS.md: the whole campaign is **still untracked in
git** (a pre-registration outside version control can't prove it predates its results),
and `boosted_moving_average.pine` has no `.meta.json` — no provenance, and it isn't even
from the strategies frame.

## 2026-08-12 — correction: consumer-goods keys were never missing

Follow-up verifying the 8/10 "key finding" below (`.env` only FRED + APININJA, 5 pipelines
still SKIP). **That finding was checked against the wrong repo.** The pipelines it listed
(`kroger`, `bestbuy`, `usda_ams`, `usda_nass_prices`, `eia_energy`) live in
`consumer-goods-price-pipeline` (separate repo), not here — and that repo's `.env` has
all the keys:

- `KROGER_CLIENT_ID` / `KROGER_CLIENT_SECRET` — `kroger_products` live: 3,523 rows /
  1,223 products / 4 store regions, fresh `20260811` pull (real Kroger/Ralphs/King
  Soopers product + price data, Certification env at api-ce.kroger.com).
- `USDA_AMS_API_KEY` — `usda_ams_retail` + `usda_ams_wholesale`: fresh `20260811` pulls.
- `USDA_NASS_API_KEY` — `usda_prices_received` + `usda_prices_paid`: fresh `20260811` pulls.
- `EIA_API_KEY` — present (consumer-goods `eia_energy_prices_pipeline.py`).

Only **Best Buy** genuinely SKIPs: `BESTBUY_API_KEY` missing — developer.bestbuy.com's
signup rejects free/.edu email addresses, deliberate not not-worked-around (see
consumer-goods TODO.md "Deferred"). Pipeline code is built + fully wired; activation is a
one-line `.env` add whenever a qualifying (non-free-provider) email + key exist.

Implication for the task list: the `financial-data-pipeline\TASKS.md` item "re-add the 8/4
API keys to .env" was mis-scoped — a `.env` edit in THIS repo would do nothing since the
pipelines aren't here. Fixed in TASKS.md to reflect reality (only Best Buy remains open).

## 2026-08-10 (cont.) — full update pass across all 5 pipelines

Follow-up to the health check below. This repo was the flagship; nothing was broken
here beyond the already-known Schwab OAuth expiry. Work done this session:

- **financial** — full test suite run: **578 passed**, 8 warnings, ~6.5 min. No
  regressions. Schwab OAuth still expired (4 `schwab_*` accumulators) — **skipped per
  user's "skip Schwab this round"**; `DAILY_ACCUMULATOR_FAIL.txt` stays until a real
  terminal re-auth. Working tree unchanged (the pre-existing uncommitted local work is
  still there, untouched).
- **consumer-goods** — reconciled (committed 16-file local CPI batch `04e0432`, pulled
  15 commits, resolved 3 conflicts in origin's favor, rewrote
  `tests/test_eurostat_hicp.py` `f876ada`, merge `1c8b122`), fixed `run_all.py` cp1252
  crash on non-ASCII (`7815e8a`), refreshed stale-format `openfoodfacts_prices`
  (287,563 rows, validate PASS), 78 tests pass, pushed `8000856` (5 commits).
  **Key finding: `.env` has only FRED + APININJA — the 8/4 USDA_AMS/NASS/EIA/KROGER
  keys are NOT in this clone**, so 5 pipelines still SKIP (see TASKS.md).
- **freight-rail** — ruff 329→0 errors + mypy + pre-commit installed, 98 tests; full
  refresh run success (4,348,342 records, all 5 sources, `run_20260810_210252_ce9278`);
  pushed `d7c0201`.
- **shipping** — `uv sync --extra dev`, 268 tests, ruff/mypy clean; fixed the
  date/datetime staleness crash (quality.py:140, alerts.py:219, freshness_sla.py) and
  the dropped `ais_positions.flag` column (schema + migration `202608100001` applied);
  refreshed 5 stale sources (1,789,777 rows); pushed `3228b29`.
- **hardware** (4th repo, previously untracked) — deps installed (pandas) + pinned
  requirements, DB init verified, BuildCores loader live-verified (5 GPU files),
  pcpartpicker client fixed to the lib's real `retrieve()` API; initial commit `64a08e8`.

Cross-cutting note: three repos (freight-rail, shipping, consumer-goods) each had a
long-lived shell-timeout pitfall — the bash tool's 120s default kills child process
trees on Windows, so long pipeline runs must be launched detached (`Start-Process` /
scheduled task) and polled. Worth encoding in each repo's CLAUDE.md.

## 2026-08-10 — all-pipeline health check (4 repos)

Health check across all data pipeline repos. Findings:

**financial-data-pipeline** — `DAILY_ACCUMULATOR_FAIL.txt` present (8/9): **4 PASS / 4 FAIL**.
- All 4 FAILs are Schwab (`schwab_quotes/options/intraday/movers`) — **OAuth refresh token expired**, each timed out waiting for interactive auth (600/1800/900/600s → ~66 min wasted). First failure after 9 straight OK days (8/1–8/8). Fix requires Zander to re-auth in a real terminal (auth code expires ~30s). Fail file auto-clears on next clean run.
- PASS: `short_interest` (30 yfinance rows, 54,960 SEC FTD rows; FINRA CDN still 403), `finnhub_events` (earnings 1,500 / insider 938 / IPO 61), `tradingview` (520 rows), `hf_sync` (180 tables, 105,146,966 rows, 2,986.9 MB, verified remotely).
- `AV Earnings Pacing` (8/9) OK: 20/20 AV budget used, real progress (+1,013 earnings, 221 dividends, 15,439 insider). Note: news/sentiment request returned invalid-input error; `top_gainers_losers` skipped on budget. Cross-repo quota coordination with `earnings_sentiment_tool` still working.
- `Fundamentals HF Refresh` (8/9) OK: full EDGAR companyfacts (20,188 companies, 2,122,746 annual / 4,661,213 quarterly rows), `build_fundamentals_dataset.py` pushed 6,026,022 rows, VERIFY PASS.
- `PipelineQuality` scheduled task last ran 8/3 (weekly Monday; next 8/10) — no `QUALITY_FAIL.txt`.
- Uncommitted local work present (not touched this session): modified `TODO.md`, `open_meteo_pipeline.py`, `storage/curated/README.md`; untracked `experiments/2026-08-07_hormuz-*`, `experiments/2026-08-08_hormuz-*`, `tests/test_open_meteo.py`. HEAD `968a16c`, in sync with origin/master.

**consumer-goods-price-pipeline** — needs reconcile. Behind origin by 15 commits with 5 modified + 10 untracked files (new: `eurostat_hicp`, `statcan_retail_prices`, `fred_cpi`, `openfoodfacts_price`, `apininja_inflation` pipelines). Last data written 8/3; `bls_cpi` had 4 failure logs that day (log tail reads "boom"). No automation scheduled.

**freight-rail-data-pipeline** — healthy. Clean, current with origin (`e085093`, 8/9). Run 8/9 21:21 UTC success (eurostat +1,329 records, total 7 sources). HF export refreshed 8/9 18:35.

**ShippingDataPipeline** — healthy. Clean, current with origin (`3af9ba2`, 8/9). `collect.yml` HF-sync step (`6695730`) now pushed to origin; gated on `secrets.HF_TOKEN`. `open_meteo` + `digitraffic` collected 8/9; `ais_positions` last 8/3 (expected snapshot); `vessels` landed ~890 rows (Session 16 fix for the stale `imo PRIMARY KEY` migration).

## 2026-08-07 (cont.) — HF token + standing rule

- **HF token added to `.env`** (`financial-data-pipeline`): appended a new `HF_TOKEN=` line with the user's write token — the pre-existing `HF_TOKEN` line was left untouched (dotenv last-wins → new token is effective). Token was already present as `HF_WRITE_TOKEN`.
- **Standing rule recorded** in `C:\Users\Zander\.claude\CLAUDE.md` (Zander standing rules): never delete or replace anything unless asked twice; prefer append/new-line additions. Applies to all future sessions.
- Note: docs commit `5029780` (session notes + TASKS.md) is on master but **not yet pushed** (local ahead 1).

## 2026-08-07 (cont.) — Ported data-integrity fixes + wired shipping HF sync

- **Financial fixes ported to master and pushed** (`e3512e3`): the `fdp-review` branch's 4 fixes were assessed against master — #2 (query dedup) and #4 (validator `period_end`/`theo_price`) already landed via `curated.py` + later `validate.py` commits, so only #3 and #6 were ported, no merge/rebase needed.
  - `fundamentals_pipeline.py`: `extract_concept` now filters XBRL facts by period duration (~3-mo for 10-Q, ~12-mo for 10-K) so quarterly flow metrics are true discrete quarters, not YTD cumulatives filed under the same `period_end`. Instant balance-sheet facts always kept; other forms (20-F, 6-K, 8-K, amendments) untouched. Master already emits `start_date`/`duration_days`, so no new column was needed (fdp-review had added `period_start`).
  - `finnhub_pipeline.py`: redacted API token from non-200/429 error logs.
  - Verified: order-independence synthetic checks (YTD listed first) pass; 101 tests pass, 2 pre-existing env failures (missing Iceberg storage dirs). Branch kept for now per user; deletion to be revisited later.
- **Shipping HF sync wired into CI** (`ShippingDataPipeline`, committed `6695730`, not pushed): added `Sync to HuggingFace` step to `collect.yml` after data-quality checks, gated on `HF_TOKEN`. Repo has NO HF_TOKEN secret yet — user must add it, then push. Local repo has no `pipeline.db`/`.env`, so a local manual refresh isn't possible without a local collect run.

## 2026-08-07 — HF dataset pipeline sync review

Audited the 4 HF datasets vs their feeding repos. Findings:
- `financial-data-pipeline` (HF): clean/current; HF sync wired into `run_all.py` (`--no-hf-sync`); HF updated ~18h ago. Healthy.
- `financial-fundamentals` (HF): last built Aug 6, but **4 data-integrity fixes still unmerged** on the `fdp-review` worktree branch `fix/data-integrity-and-secrets`: #2 query-layer dedup, #3 discrete-quarter XBRL fundamentals (fixes YTD bug in quarterly tables pushed to HF), #4 validator schema drift, #6 Finnhub token leak in logs. Branch base `9cb0c4a` is behind current master `b81e65a` → needs rebase before merge.
- `freight-rail-data-pipeline`: **local clone 7 commits behind origin/main** — origin holds the sources that produced the current HF tables (`bts_freight_indicators`, `fmcsa_carrier_census`, `fra_safety`, USDA GTR grain) plus `upload_huggingface.py`. Local uncommitted WIP diverges in the SAME files origin also changed (normalizer.py, schemas.py, freightos_fbx.py, usda_agtransport.py, storage.py) → needs rebase/reconcile. USDA Socrata resource-ID conflict: local WIP = `tb7q-kn5i`/`axkm-yjzy` vs origin = `swcm-ytjc`/`jvfn-6e7j` — **unverified which is correct**. Stray `=` scratch file present (failed Socrata test). HF last synced Aug 3.
- `ShippingDataPipeline`: clean/current, but **HF sync gap** — `collect.yml` collects daily in CI yet never uploads to HF; HF dataset last updated Aug 3.

Decision: session notes + task list live in `financial-data-pipeline` (repo with the SESSION_NOTES convention). No code changed this session — review only.

**Discovered a second diverging local clone.** Continuing eval-framework/backlog work
(see `financial-data-pipeline/FinancialDataPipeline_Future_Improvements.md` items P/G/H)
had all been happening in `C:\Users\zande\financial-data-pipeline`, a *different* local
checkout from this one (`C:\Users\zande\PycharmProjects\financial-data-pipeline`) that
also tracks `Zanderl1987/financial-data-pipeline`. That other clone's `storage/raw/` is
thin (5.1MB — only pipelines run directly in it), while **this** clone holds the real
27,759-symbol Schwab full-universe backfill (`prices`, 46.9M rows, 4.2GB). This clone was
also 8 commits behind `origin/master` and had its own uncommitted local edits (an AV
earnings-pacing log entry, an HF re-sync record, a new `TODO.md`).

Resolved by: committing this clone's local edits (`4f59fa4`), merging `origin/master`
in (`bc9caef`, one conflict in `SESSION_NOTES.md` — both sides had added a same-day
entry; kept both), then fixing a `test_storage_dirs_exist` failure caused by 6 tables
(`dark_pool_volume`, `retail_sentiment`, `retail_sentiment_daily`, `insider_sentiment`,
plus the 3 new `indeed_job_postings_*` tables) that were merged in from origin but have
never been run in *this* clone, so their storage dirs didn't exist yet — same fix
pattern as before (`.gitkeep` placeholders). Full suite: 474 passed. Pushed `56358f5`.
**Both clones are now in sync with `origin/master`.** No action taken to prevent this
recurring — worth remembering next session that two local clones of this repo exist on
this machine, and to check `git status`/`git log -1` in whichever one you're about to
work in before assuming it's current.

**Full-universe factor-validation design (in progress, not yet written to a spec).**
User asked what to build next with the backtesting engine; landed on validating
`momentum`/`low_vol` (the only two factors with real full-universe breadth — everything
else needs fundamentals/short-interest/insider/sentiment data that only covers the
69-symbol watchlist) against the full ~13,219-symbol exchange-listed universe
(`symbol_universe.csv`, excluding OTC Markets/Nasdaq OTCBB) instead of the watchlist.
User explicitly asked to avoid survivorship bias; scoped that into two distinct issues:
(1) look-ahead from using *today's* liquidity to judge history — solvable with a
point-in-time trailing dollar-volume filter; (2) delisted/bankrupt companies being
entirely absent from `prices`/`symbol_universe.csv` (a 2026-07-24 snapshot of
currently-tradable Schwab instruments) — NOT solvable without different source data,
to be documented as an explicit limitation, not fixed.

Proposed architecture (posted to user, awaiting confirmation before writing the spec):
new `evaluation/universe.py` (`exchange_listed_symbols()` + `point_in_time_eligible()`,
a single DuckDB rolling-dollar-volume query with no lookahead) + an additive
`eligible=` param on `evaluation/adapters.py::from_signal_panel()` (default `None` =
unchanged behavior) + two new opt-in CLI flags on `evaluate.py` (`--exclude-otc`,
`--min-dollar-volume`). Key finding that shaped the design: `ic.py`/`stats.py` already
operate per-date on whatever rows are in the panel, so "point-in-time dynamic universe"
needs zero changes to the tested core (`data.py`/`ic.py`/`stats.py`/`runner.py`) — it's
purely a Signal-construction concern. Acceptance plan: run momentum + low_vol over the
filtered universe, compare against existing watchlist baselines via
`registry.compare(allow_universe_mismatch=True)` (already exists for exactly this).
**Next step:** get user sign-off on the design, then write
`docs/superpowers/specs/2026-07-29-full-universe-factor-validation-design.md` and
proceed to an implementation plan.

---

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

## 2026-07-28 — AV earnings pacing attempt + session task list created

Ran `alpha_vantage_fundamentals_pipeline.py` in default incremental mode. All 20
requests returned the Alpha Vantage "Information" (quota exhausted) message —
`custom_index_tool` automations likely consumed the daily 25-request quota before
this run. Still at 9/30 DJI symbols covered. Will retry later or tomorrow.

Also created `TODO.md` with 6 prioritized items from PROJECT_NOTES.md open work.

### HF dataset published to HuggingFace

Ran `upload_huggingface.py`. Uploaded 148 tables, 59,291,129 rows, 2,208.1 MB
to `https://huggingface.co/datasets/ZanderL1337/financial-data-pipeline` (public,
updated from 2026-07-19's 114 tables / ~10M rows / 223.6 MB). Upload took ~17 min
for the 2.05 GB `prices.parquet` file alone.

### Fed SOMA full backfill completed

Ran `fed_soma_pipeline.py --backfill` with extended timeout. Fetched 1,203 weekly
report dates (2003-07-09 to 2026-07-22) for both tsy and agency asset types:
- Treasury: 353,016 rows
- Agency (MBS): 30,462,197 rows
- Total: 30,815,213 rows
Curated + validated PASS (148 PASS / 88 NO DATA). File: 345 MB.

### Patents rewrite — blocked (needs ODP API key)

Investigated USPTO ODP API (`https://api.uspto.gov`); requires `X-API-KEY`
header and a USPTO.gov account. No key in `.env`. Skipped until Zander
registers for one.

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

---
## 2026-07-30 � ETF Holdings Pipeline + Fund Holdings Expansion

### What was built
1. **etf_holdings_pipeline.py** � fetches full holdings for 200+ US ETFs from SecuritiesDB free API (no key, no auth). Writes to Iceberg table constituents.etf_holdings with per-fund-ticker partition overwrite. Live verified: 299 rows (SPY 99, IVV 100, VOO 100) written.
2. **fund_holdings_pipeline.py expanded**: ETF_PID_MAP 17 ? 65 iShares ETFs; MUTUAL_FUND_UNIVERSE 10 ? 52 mutual funds.
3. **Wired etf_holdings** into validate.py, curated.py, query.py, run_all.py, test_catalog.py, test_pipelines.py. 155/155 wiring tests pass.
4. **Wired error logging** into both write_to_iceberg() functions: catalog load, Arrow schema conversion, per-ticker overwrite, and verification query all wrapped in try/except.

### Key findings
- SecuritiesDB (securitiesdb.com/api/v1/etfs/{ticker}/holdings) works with no auth.
- BlackRock iShares PIDs from etf-scraper listings.csv on GitHub.
- PyIceberg on Windows needs DoubleType (not FloatType) to match pa.float64().
- This session worked from C:\Users\zande\financial-data-pipeline (not PycharmProjects version).

---
## 2026-07-30 � ETF Holdings Pipeline + Fund Holdings Expansion

### What was built
1. **etf_holdings_pipeline.py** � fetches full holdings for 200+ US ETFs from SecuritiesDB free API (no key, no auth). Writes to Iceberg table constituents.etf_holdings with per-fund-ticker partition overwrite. Live verified: 299 rows (SPY 99, IVV 100, VOO 100) written.
2. **fund_holdings_pipeline.py expanded**: ETF_PID_MAP 17 -> 65 iShares ETFs (factor, sector, international, ESG, multi-asset, dividend, commodities, RE, short duration); MUTUAL_FUND_UNIVERSE 10 -> 52 mutual funds (Vanguard, Fidelity, Schwab, PIMCO, American Funds, T. Rowe Price, DFA).
3. **Wired etf_holdings** into validate.py SCHEMAS, curated.py KEYS, query.py CATALOG, run_all.py PipelineSpec, test_catalog.py, test_pipelines.py. 155/155 wiring tests pass.
4. **Wired error logging** into both write_to_iceberg() functions: catalog load, Arrow schema conversion, per-ticker overwrite, and verification query all wrapped in try/except with log.error()/log.warning().

### Key findings
- SecuritiesDB (securitiesdb.com/api/v1/etfs/{ticker}/holdings) works with no auth, returns up to ~500 holdings per ETF.
- BlackRock iShares PIDs can be bulk-discovered from etf-scraper's listings.csv on GitHub (raw.githubusercontent.com/nikulpatel3141/ETF-Scraper/main/src/etf_scraper/data/listings.csv).
- PyIceberg on Windows needs DoubleType (not FloatType) to match pa.float64(). Also crashes on Windows terminal when rendering schema-diff Unicode tables (cp1252).

### Repo note
This session worked from C:\Users\zande\financial-data-pipeline. The CLAUDE.md and previous sessions use C:\Users\zande\PycharmProjects\financial-data-pipeline. Both copies exist; check which one before running.
