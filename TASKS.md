# Tasks

## Needs action (blocked on user)
- [ ] financial: re-auth Schwab OAuth token (expired 8/9 — all 4 `schwab_*` pipelines timed out in the daily accumulator run; first failure after 9 OK days). Interactive: run in a real terminal, auth code expires ~30s. `DAILY_ACCUMULATOR_FAIL.txt` auto-clears on next clean run.
- [ ] consumer-goods: reconcile repo — behind origin by 15 commits with 5 modified + 10 untracked files (new pipelines: `eurostat_hicp`, `statcan_retail_prices`, `fred_cpi`, `openfoodfacts_price`, `apininja_inflation`). Rebase, don't lose the local work, then decide on the bls_cpi failures (4 failure logs on 8/3).
- [ ] financial: decide fate of uncommitted local work (modified `TODO.md`, `open_meteo_pipeline.py`, `storage/curated/README.md`; untracked `experiments/2026-08-0[78]_hormuz_*` + `tests/test_open_meteo.py`) — commit or discard before next run.
- [ ] REMINDER (deferred): revisit deleting the now-superseded `fix/data-integrity-and-secrets` branch once confident the ported fixes are complete.

## Verify (can't confirm locally)
- [ ] shipping: confirm `HF_TOKEN` write-token secret is set on the ShippingDataPipeline repo — `collect.yml` HF-sync step (now pushed as `6695730`) is gated on `env.HF_TOKEN != ''`, so a missing secret silently skips the sync.

## Done (as of 2026-08-10 health check)
- [x] freight-rail: local clone reconciled to origin/main and HF export refreshed (repo clean/current, data run 8/9, `data/hf_export/*.parquet` rewritten 8/9 18:35)
- [x] freight-rail: USDA Socrata resource-ID dispute resolved by reconcile — origin IDs (`swcm-ytjc`/`jvfn-6e7j`) are what's now in the repo (local WIP IDs dropped); original "which is correct" verification still technically unconfirmed
- [x] freight-rail: stray `=` scratch file gone
- [x] shipping: HF sync step wired into `collect.yml` AND pushed to origin (`6695730`; origin/main = `3af9ba2` as of 8/9)
- [x] financial: docs commit `5029780` pushed — master is in sync with origin/master (HEAD `968a16c`)
