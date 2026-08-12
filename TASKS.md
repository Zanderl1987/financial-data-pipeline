# Tasks

## Needs action (blocked on user)
- [ ] consumer-goods: **re-add the 8/4 API keys to `.env`** — `.env` currently holds only `FRED_API_KEY` + `APININJA_API_KEY`; the USDA_AMS / USDA_NASS / EIA / KROGER keys documented as activated on 8/4 are NOT in this clone, so `usda_ams`, `usda_nass_prices`, `eia_energy`, `kroger`, `bestbuy` SKIP at runtime. One `.env` line each unblocks them (code identical post-merge). Values presumably live in whichever clone/working tree did the 8/4 activation.
- [ ] financial: decide fate of uncommitted local work (modified `TODO.md`, `open_meteo_pipeline.py`, `storage/curated/README.md`; untracked `experiments/2026-08-0[78]_hormuz_*` + `tests/test_open_meteo.py`) — commit or discard before next run. (Unchanged on 8/10.)
- [ ] freight-rail: register `FRED_API_KEY` (fred.stlouisfed.org/docs/api/api_key.html) then run `freight-pipe run --sources fred` — the FRED source was built 8/9 but has never run against a real key. Also open: Freightos FBX signup, EIA/BLS/Census/UN Comtrade keys (see `US_DOMESTIC_FREIGHT_SOURCES.md`).
- [ ] shipping: obtain `EIA_API_KEY`, `UN_COMTRADE_API_KEY`, `OILPRICEAPI_API_KEY` to finish the key-gated 0-row tables (`oil_inventories`, `trade_flow`, `oil_prices`/`freight_rates`).
- [ ] REMINDER (deferred): revisit deleting the now-superseded `fix/data-integrity-and-secrets` branch once confident the ported fixes are complete.

## Verify (can't confirm locally)
- [ ] shipping: confirm `HF_TOKEN` write-token secret is set on the ShippingDataPipeline repo — `collect.yml` HF-sync step (pushed as `6695730`) is gated on `env.HF_TOKEN != ''`, so a missing secret silently skips the sync.
- [ ] freight-rail: re-run `upload_huggingface.py` — HF dataset (synced 8/9) is one run behind the local store (7 tables refreshed to 4,348,342 records on 8/10) and still lacks the motor-carrier-census table from the PR #1 merge.

## Done (as of 2026-08-11 update pass)
- [x] financial: re-auth Schwab OAuth token — DONE (2026-08-11). Expired refresh token re-established via the local capture listener (then `scripts\schwab_local_reauth.py`; **since merged into `scripts\schwab_reauth.py`** — use that for future re-auths, the old file is deleted — HTTPS on 127.0.0.1:8182, launched detached under Task Scheduler); token issued 2026-08-11 22:46 UTC, valid to 2026-08-18. Also filled the 08-10/08-11 data gap (8 PASS / 0 FAIL run_all, HF re-synced: 185 tables / 106M rows / 3.0 GB) and added `--incremental` to `schwab_universe_backfill.py` + nightly task `SchwabUniverseIncrementalPrices` (22:00, keeps the 27,759-symbol universe current). See SESSION_NOTES_2026-08-11.md. `DAILY_ACCUMULATOR_FAIL.txt` cleared.
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
