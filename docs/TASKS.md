# Tasks

## Needs action (blocked on user)
- [ ] consumer-goods: obtain a **Best Buy API key** (`BESTBUY_API_KEY`) — the last genuinely-SKIPping pipeline. developer.bestbuy.com's signup rejects free/.edu email addresses; revisit when a non-free-provider email is available. Code is built + fully wired; activation is one `.env` line in `consumer-goods-price-pipeline`. See consumer-goods docs/TODO.md "Deferred".
- [ ] freight-rail: register `FRED_API_KEY` (fred.stlouisfed.org/docs/api/api_key.html) then run `freight-pipe run --sources fred` — the FRED source was built 8/9 but has never run against a real key. Also open: Freightos FBX signup, EIA/BLS/Census/UN Comtrade keys (see `US_DOMESTIC_FREIGHT_SOURCES.md`).
- [ ] shipping: obtain `EIA_API_KEY`, `UN_COMTRADE_API_KEY`, `OILPRICEAPI_API_KEY` to finish the key-gated 0-row tables (`oil_inventories`, `trade_flow`, `oil_prices`/`freight_rates`).
- [ ] REMINDER (deferred): revisit deleting the now-superseded `fix/data-integrity-and-secrets` branch once confident the ported fixes are complete.

## Verify (can't confirm locally)
(none open)

## Done (as of 2026-08-13 update pass)
- [x] shipping: confirmed via `gh secret list` — `HF_TOKEN` IS set on the ShippingDataPipeline repo (alongside `AISSTREAM_API_KEY`, `EIA_API_KEY`), so `collect.yml`'s HF-sync step is not silently skipping.
- [x] freight-rail: already resolved by other work before this pass — repo HEAD (`e5a00cf`, 2026-08-13) shows a BTS TransBorder backfill + HF re-sync landed same day; `data/hf_export/*.parquet` includes `motor_carrier_census.parquet` (16.0M). No action needed.
- [x] consumer-goods: pulled 6 commits the local clone was behind on (fast-forward `3188590..4396b96`), committed + pushed the untracked 2026-08-12 verification session notes (`7e4713a`). All three repos (financial, consumer-goods, earnings_sentiment_tool) confirmed clean and synced with origin; hardware-pipeline also clean.
- [x] financial / TV catalog: `storage/tv_scripts/boosted_moving_average.pine` deleted per Zander's explicit call — it was untracked, had no `.meta.json`, and never came from the strategies sampling frame.

## Done (as of 2026-08-12 update pass)
- [x] financial / TV catalog: **campaign committed to git** (`91be7d3`) — `strategies/` (collect.py + screen.py + tests), `storage/tv_scripts/` (6 .pine, 5 meta.json, both sampling rosters), the pre-registration + amendments, and session notes are now tracked. `boosted_moving_average.pine` deliberately left untracked (provenance gap, open decision). Also committed: the long-open "uncommitted local work" item is resolved — the dataset-card refresh (`c2fe2ad`); docs/TODO.md / open_meteo_pipeline.py / hormuz experiments / test_open_meteo.py had already landed in earlier commits.
- [x] financial / TV catalog: Batch 1 collection advanced from 1 to **6 scripts collected, 5 admitted to Stage 2** (new: `rabiah6x_ut_bot_scalper` — excluded `unconfirmed_htf`; `supertrend_entry_tp123`; `vegas_channel_tunnel_v11`; `hybrid_breakout_vcp`). Full 23-entry sampling roster with per-slug status saved to `storage/tv_scripts/_roster_strategies_popular_2026-08-12.txt` (13 TODO) so the site never needs re-enumerating. New pre-registration amendment records a ~300-line collection-size limit and the two escape hatches that were tested and closed (`btoa()` returns are filter-blocked; script pages are client-rendered so a plain HTTP fetch has no source). Screener tests 24/24. See `docs/sessions/SESSION_NOTES_2026-08-12_tv-catalog.md`. Still Stage 0/1 only — nothing translated, no endpoint computed.
- [x] consumer-goods: **verified keys were never missing** — the 8/10 "only FRED+APININJA in .env" finding was checked against the wrong repo. The kroger/usda_ams/usda_nass/eia pipelines live in `consumer-goods-price-pipeline` and all keys are in that repo's `.env`, with fresh 2026-08-11 pulls (kroger_products 3,523 rows / 1,223 products / 4 regions; both usda_ams tables; both usda_nass tables). Only Best Buy genuinely SKIPs (signup rejects free/.edu email). See docs/sessions/SESSION_NOTES.md 2026-08-12 entry.

## Done (as of 2026-08-11 update pass)
- [x] financial: re-auth Schwab OAuth token — DONE (2026-08-11). Expired refresh token re-established via the local capture listener (then `scripts\schwab_local_reauth.py`; **since merged into `scripts\schwab_reauth.py`** — use that for future re-auths, the old file is deleted — HTTPS on 127.0.0.1:8182, launched detached under Task Scheduler); token issued 2026-08-11 22:46 UTC, valid to 2026-08-18. Also filled the 08-10/08-11 data gap (8 PASS / 0 FAIL run_all, HF re-synced: 185 tables / 106M rows / 3.0 GB) and added `--incremental` to `schwab_universe_backfill.py` + nightly task `SchwabUniverseIncrementalPrices` (22:00, keeps the 27,759-symbol universe current). See docs/sessions/SESSION_NOTES_2026-08-11.md. `DAILY_ACCUMULATOR_FAIL.txt` cleared.
- [x] consumer-goods: repo reconciled with origin — local CPI batch committed (`04e0432`), 15 commits pulled, 3 conflicts resolved (eurostat_hicp / openfoodfacts / statcan_retail, origin's versions), `tests/test_eurostat_hicp.py` rewritten (`f876ada`), merge `1c8b122`.
- [x] consumer-goods: `run_all.py` cp1252 crash on non-ASCII fixed (`7815e8a`); stale-format `openfoodfacts_prices` refreshed (287,563 rows, curated + validate PASS); 78/78 tests pass; all 5 commits pushed (`8000856`).
- [x] freight-rail: gate cleanup — ruff 329 errors → 0 (per-file S101/S110 ignores + manual F841/E501/import fixes), mypy clean, pre-commit installed, 98 tests pass; pushed `d7c0201`.
- [x] freight-rail: full data refresh succeeded (`run_20260810_210252_ce9278.json`, success=true, 4,348,342 records, all 5 sources).
- [x] shipping: `uv sync --extra dev`; 268 tests pass; ruff/mypy clean; fixed date/datetime staleness crash (quality.py, alerts.py, freshness_sla.py) and dropped `ais_positions.flag` column (schema + migration `202608100001` applied); refreshed 5 stale sources (1,789,777 rows); pushed `3228b29`.
- [x] hardware-pipeline: deps installed + requirements pinned, DB init + BuildCores loader live-verified, pcpartpicker client fixed to real `retrieve()` API; initial commit `64a08e8`.
- [x] freight-rail: local clone reconciled to origin/main and HF export refreshed (repo clean/current, data run 8/9, `data/hf_export/*.parquet` rewritten 8/9 18:35)
- [x] freight-rail: USDA Socrata resource-ID dispute resolved by reconcile — origin IDs (`swcm-ytjc`/`jvfn-6e7j`) are what's now in the repo (local WIP IDs dropped); original "which is correct" verification still technically unconfirmed
- [x] freight-rail: stray `=` scratch file gone
- [x] shipping: HF sync step wired into `collect.yml` AND pushed to origin (`6695730`; origin/main = `3af9ba2` as of 8/9)
- [x] financial: docs commit `5029780` pushed — master is in sync with origin/master (HEAD `968a16c`)
