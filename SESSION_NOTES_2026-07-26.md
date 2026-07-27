# Session Notes — 2026-07-26

**Branch:** master
**Session model:** Claude Sonnet 5

## What happened

Session started as a "continue where we left off" across three repos (this one,
`custom_index_tool`, `earnings_sentiment_tool`) with no prior conversation history —
reconstructed state from memory + live inspection rather than trusting stale notes.

Found and fixed everything blocking `QUALITY_FAIL.txt` (flag was from the weekly
2026-07-20 run: 96 PASS / 18 FAIL / 117 NO DATA). Rerunning `validate.py` fresh showed
most of the 18 had already self-resolved via 07-23 data refreshes (confirms the
2026-07-14 staging batch — see that date's entry below in repo history — got applied
sometime between 07-20 and 07-23, undocumented anywhere). Only 7 real fails remained,
tracing to 4 distinct root causes, all fixed and verified live (commits `79d709c`,
`0ed73fa`):

1. **`validate.py` picked the "latest" file alphabetically, not by date.** Old
   differently-named leftover files (e.g. `coins_markets_2026-07-13.parquet`, from
   before a `fetched_at` column existed) sorted AFTER current correctly-named files
   (`'coins_' > 'coingecko_'` lexicographically), so validation kept reading stale
   garbage next to perfectly good fresh data. Fixed via a shared `_latest_file()`
   helper that sorts by `os.path.getmtime` instead. **Systemic bug — any table with a
   renamed/reformatted pipeline output was at risk of this.** Fixed
   `coingecko_coins_markets`/`trending`/`exchange_rates`/`global_market`.
2. **`eia_petng_prices_pipeline.py` silently dropped `product`/`product-name`** even
   though EIA's `petroleum/sum/snd` endpoint returns them on every row (confirmed via
   a live API call) — was collapsing distinct products (crude/gasoline/asphalt/etc.)
   into process+date rows with no way to tell them apart. `validate.py`'s
   `product_code` schema requirement was correct all along; the pipeline was silently
   wrong. Fixed to capture and keep both columns; rebackfilled (48,628 rows, 54
   distinct products, 0% null).
3. **`coingecko_expansion_pipeline.py`'s `fetch_derivatives()`** read
   `ticker["exchange"]["name"]`, but CoinGecko's `/derivatives` response has no nested
   `"exchange"` object — exchange name is a flat `"market"` string. Rewrote the row
   mapping against a live sample response; dropped several always-null phantom fields
   (`base`/`target`/`index_price`/`last_updated`) and added real ones the API actually
   returns (`index_id`, `contract_type`, `open_interest`, `volume_24h`, etc.).
4. **`finnhub_expansion_pipeline.py`'s `FILING_RENAME`** expected
   `filingDate`/`url`/`primaryDocument`, but Finnhub's actual `stock/filings` response
   uses `filedDate`/`reportUrl`/`filingUrl` (confirmed against cached raw data);
   `accessNumber` was never snake_cased either. Fixed and rebackfilled (7,500 rows
   across 30 DJI symbols, 0% null `filing_date`).

Verified each fix by rerunning the affected pipeline live, rebuilding curated
snapshots, and confirming PASS in `validate.py`. Full suite: 453 passed. Reran the
actual weekly quality-check wrapper script (not a hand-edit) — it regenerated a fresh
report and auto-cleared `QUALITY_FAIL.txt` on its own: **148 PASS, 0 FAIL, 83 NO DATA.**

## Known pre-existing issue, NOT fixed this session

`tests/test_catalog.py::TestCatalogPaths::test_storage_dirs_exist` fails — missing
`storage/raw/` directories for 6 tables whose pipelines are wired into `run_all.py`
CATALOG but have apparently never actually been run: `omkar_commodity`,
`fed_speeches`, `fed_sentiment`, `alpha_vantage_income_statement`,
`alpha_vantage_balance_sheet`, `alpha_vantage_cash_flow`. Not caused by anything in
this session; flagged for a future pass (either run those pipelines once or address
why they were wired without ever running).

## Session 2 (same day): `test_storage_dirs_exist` resolved

Diagnosed the 6 missing-storage-dir tables flagged above. None were bugs — all three
distinct causes were "pipeline has genuinely never been run," each for a reason that
needed a user decision rather than a silent fix:

1. `omkar_commodity` — needs `OMKAR_API_KEY`, never added to `.env`.
2. `fed_speeches`/`fed_sentiment` — needs `ANTHROPIC_API_KEY`, never added to `.env`
   (matches the known gap already noted in CLAUDE.md).
3. `alpha_vantage_income_statement`/`balance_sheet`/`cash_flow` — only populate via
   `alpha_vantage_fundamentals_pipeline.py --backfill`, which the script's own
   docstring calls "uncapped — vastly exceeds daily quota, expect multi-day runtime."
   Running it would compete for Alpha Vantage's shared 25-req/day-per-IP budget
   against `earnings_sentiment_tool`'s transcript and earnings-surprise pulls.

User decided: **deliberately unwire** the first three (keys not being added right
now), and **skip the AV backfill for now** rather than run it and collide with the
other repo's quota. Implemented:

- Commented out (not deleted) `omkar_commodity` / `fed_speeches` / `fed_sentiment`
  wiring in `query.py` CATALOG, `run_all.py` PipelineSpecs, `validate.py` SCHEMAS,
  `curated.py` KEYS, and `tests/test_catalog.py` EXPECTED_TABLES — each site has a
  `# Unwired 2026-07-26: requires <KEY>, never set` comment so it's a one-line
  re-enable if the key is ever added. Pipeline source files (`omkar_commodity_pipeline.py`,
  `fed_sentiment_pipeline.py`) left untouched on disk.
- Added a `NOT_YET_BACKFILLED` set to `TestCatalogPaths` in `tests/test_catalog.py`
  that `test_storage_dirs_exist` skips, scoped to just the 3 AV statement tables,
  with the quota-collision reasoning in a comment — not a blanket weakening of the
  test.

Verified: `tests/test_catalog.py` 13/13 pass, `run_all.py --dry-run` plan no longer
lists the unwired pipelines, `validate.py` runs clean (148 PASS / 0 FAIL / 80 NO DATA,
down from 83 NO DATA — exactly the 3 removed tables). Full suite confirmed clean:
**454 passed, 0 failed** (was 453 passed / 1 failed). Committed `2463c79`, pushed.

## Session 3 (same day): logging framework built

User asked for "tests and logging framework for errors" since none existed —
confirmed only 4 of ~90 pipeline files used Python's `logging` module at all (console
only, no file persistence), and `run_all.py` only ever kept a one-line "exit 1" note
per failure, discarding the actual stdout/stderr. Scoped with the user: this repo only
(not `earnings_sentiment_tool`), a real structured file-logging layer (not just
patching `run_all.py`'s one-liner), tests for the framework itself (not a pipeline-by-
pipeline error-path retrofit).

Built `logging_utils.py` (repo root, same tier as `storage_utils.py`):
`get_logger(name)` — console + `RotatingFileHandler` (5MB x 5 backups) at
`storage/logs/<name>.log`, idempotent (safe at import time, no duplicate handlers).
`log_pipeline_failure(name, detail)` — writes a full-output snapshot to
`storage/logs/failures/<name>_<timestamp>.log`, kept separate from the rotating log so
a specific failure's full context survives rotation.

Wired into `run_all.py`: `run_pipeline()` now uses `subprocess.run(..., capture_output=
True, text=True)` instead of inheriting stdout/stderr — output is printed after the
pipeline finishes (buffered per-pipeline, **not** streamed live character-by-character;
a deliberate tradeoff so a run is always fully captured to disk, even a silent hang
that times out with zero console output otherwise) and always handed to the logger. On
FAIL or timeout, the captured output is persisted via `log_pipeline_failure()` and its
path appended to `RunResult.note` (e.g. `exit 1 -- log: storage\logs\failures\
foo_20260726_235144.log`) instead of just `exit 1`. `_print_summary()` also logs the
run-level PASS/FAIL/SKIP counts to `storage/logs/run_all.log` on every run.
`storage/logs/` added to `.gitignore`.

Tests: `tests/test_logging.py` (12 tests) — logger has both handler types, idempotent,
writes expected content/level to file, rotation config (5MB/5 backups),
`log_pipeline_failure` content/path correctness, and 3 `run_all.py` integration tests
using real throwaway pipeline scripts (nonzero exit, timeout with partial output,
success leaves no failure log) verifying `RunResult.note` actually contains a working
log path. All isolated from the real `storage/logs/` via a `tmp_path`-monkeypatched
`LOG_DIR`/`FAILURE_DIR` **and** a rebound `ra.log` (the module-level logger is bound at
import time, before any fixture runs — patching `lu.LOG_DIR` alone doesn't stop
`run_all.py`'s own `log.error()` calls from hitting the real file; caught this via a
leaked line in the real `storage/logs/run_all.log` during the first test run, fixed,
cleaned up the leak).

Live-verified end-to-end: ran the real (keyless) `ecb` pipeline through
`run_all.py --only ecb` — console output still visible, `storage/logs/run_all.log`
got the INFO summary line, PASS as expected. Full suite: **466 passed, 0 failed**
(454 from Session 2 + the 12 new `test_logging.py` tests — exact match, confirming
nothing else broke).

Also refreshed CLAUDE.md's stale "133 CATALOG tables" line (now 148 PASS/80 NO DATA)
and documented `logging_utils.py` in its Architecture section while touching the file.

**Explicit limitation, not yet done**: individual `*.py` pipeline files were NOT
retrofitted to call `get_logger()` — they still use bare `print()` or their own
`logging.basicConfig()`. This covers the `run_all.py` orchestrator level only. A
manually-run pipeline (outside `run_all.py`) still has no persisted log.

## State / Next Up

- Cross-repo: `earnings_sentiment_tool`'s `label_join.py` depends on this repo via
  `FDP_REPO_PATH` for CAR computation — no action needed here, just noting the link
  survived the 2026-07-20 split of the NLP project out of `custom_index_tool`.
- If useful later: retrofit individual pipeline files to `logging_utils.get_logger()`
  so manual (non-`run_all.py`) runs also get persisted logs — deliberately out of
  scope for the 2026-07-26 build (see Session 3 limitation above).
