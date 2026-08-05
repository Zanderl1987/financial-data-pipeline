# Session Notes — 2026-08-04

**Branch:** master (no commits this session; local repo 14 ahead of origin, unchanged)
**Session model:** DeepSeek V4 Flash

## What happened

User asked to update the public HuggingFace dataset
`ZanderL1337/financial-fundamentals` (SEC EDGAR fundamentals, last refreshed
2026-06-15) using the financial-data-pipeline, then to make append (not
overwrite) the default HF behavior going forward.

### 1. Dataset status check

- Confirmed via HF API that the dataset is **public** (`"private": false,
  `"gated": false`) — no action needed to expose it. Siblings are just the two
  `fundamentals_*_latest.parquet` files plus `.gitattributes`.
- Mapped it to the pipeline: `fundamentals_pipeline.py --full-market` downloads
  SEC's companyfacts.zip (~15k companies), extracts 11 metrics into annual +
  quarterly parquet, pushes both as `fundamentals_annual_latest.parquet` /
  `fundamentals_quarterly_latest.parquet`.
- `.env` already has `HF_TOKEN` + `HF_DATASET_REPO=ZanderL1337/financial-fundamentals`.

### 2. Full-market refresh (the update)

- **Gotcha found:** the pipeline's HF-cache short-circuit (`hf_pull` in
  `fundamentals_pipeline.py:478`) returns early if BOTH latest files already
  exist on HF — so a plain run would have just re-pulled the cache and exited
  with no update. **`--no-cache` is required** to force a fresh download.
- Ran: `fundamentals_pipeline.py --full-market --no-cache
  --hf-repo ZanderL1337/financial-fundamentals`
- Downloaded 1,329 MB companyfacts.zip, stream-processed all 20,151 company
  JSONs (0 failed), merged to:
  - `storage/raw/fundamentals/annual/year=2026/month=08/fundamentals_full_annual_20260804.parquet` (1,378,849 rows)
  - `storage/raw/fundamentals/quarterly/year=2026/month=08/fundamentals_full_quarterly_20260804.parquet` (2,786,451 rows)
- Both pushed to HF. API now shows `lastModified: 2026-08-04T23:36:28Z`,
  commit `1bedd7c`.
- Temp `companyfacts_edgar.zip` cleaned up by the script (also confirmed no
  stale copy pre-existed in `%TEMP%`, so the download was genuinely fresh).

### 3. Curated rebuild

- Ran `curated.py` after the raw write (repo rule: direct pipeline runs must be
  followed by curated rebuild or analytics read stale data).
- 179 tables compacted, 16,415,724 duplicate rows removed (15.0% of
  109,368,710). Notable dedup: `fundamentals_annual` -33.4%, `fundamentals_quarterly` -60.9%
  (both had overlap with prior full-market + DJI-mode raw files).
- Verified fresh data landed in curated:
  - `fundamentals_annual.parquet`: 2,611,794 rows, `fetched_at` max 2026-08-04
  - `fundamentals_quarterly.parquet`: 3,330,614 rows, `fetched_at` max 2026-08-04

### 4. The "old snapshot is gone" scare → combined rebuild (RECOVERY)

- The old HF snapshot (2026-06-15, ~8.2M rows combined) was NOT on disk under
  the 2026-06-15 partition naming, so it initially looked lost. Recovered two
  ways:
  - The old data was actually intact in local raw partitions
    (`full_20260615`, `20260623`, `20260723`) under
    `storage/raw/fundamentals/{annual,quarterly}/year=2026/month=*/`.
  - HF git history commits `a5741286`/`b7a71592` held the same files.
- **User decision:** no data was lost; keep ALL historical data, restatement
  versions preserved. Combined rebuild concatenated every raw partition and
  dropped only exact full-row duplicates:
  - annual: 2,523,611 + 8,474 + 8,482 + 1,378,849 → **3,917,990 rows**
  - quarterly: 5,709,371 + 2,116 + 10,401 + 2,786,451 → **8,490,726 rows**
- Combined files (kept in temp) pushed to HF at commit
  `4f4bdd8601b1d528f46ecadd0ae54838bd9f665b`; tree sizes 27,669,015 /
  59,181,885 bytes. datasets-server reports **12,408,716 rows total**.
- Note: `resolve/main` 404s on these files are only HF's index lag; tree/API and
  `hf_hub_download` confirm presence.

### 5. Append-mode work (DONE this session)

- Added `--append` flag to `fundamentals_pipeline.py` with `hf_append()`
  helper (pull current HF file → concat → dedup → push union). Tested live as a
  no-op (3,917,990 + 3,917,990 → 3,917,990); `py_compile` OK;
  `tests/test_pipelines.py` 142/142 pass.
- **User decisions locked in:**
  - Append becomes the DEFAULT for both `--full-market` and DJI modes (both
    modes append to HF; no separate `--replace` needed unless wanted later).
  - Dedup key = **all columns except `fetched_at`**, keep newest row
    (`drop_duplicates(subset=<all-but-fetched_at>, keep="last")`). Restatement
    history survives; only re-fetches of identical facts collapse.
  - Post-update scan = HF verify (re-pull + row-count/date-range/dup-rate
    sanity) **plus** a full `validate.py` sweep.

**Implementation completed:**
- `hf_append` dedup fixed: sorts by `fetched_at` (NaT first) then
  `drop_duplicates(subset=all-but-fetched_at, keep="last")`; falls back to
  full-row dedup if `fetched_at` is missing. Dedup behavior unit-verified
  (dup row keeps newest fetch, unique old/new rows preserved).
- Append is now ALWAYS the push path in `--full-market` mode (the `if append:`
  branch removed) and `hf_append` is wired into DJI mode's output path too
  (DJI mode previously never pushed to HF). `--append` CLI flag removed
  (`hf_push` kept for a possible future `--replace`).
- **New `verify_hf.py`** (repo root): re-pulls both latest files straight from
  HF and checks row counts vs baseline, `fetched_at` recency, symbol coverage,
  and dup rate under the all-but-fetched_at key. Fails exit non-zero on stale
  data, missing columns, or dup rate > 10%.

### 5b. One-time dedup cleanup of the live HF dataset (DONE)

- `verify_hf.py`'s first live run exposed that the combined rebuild (commit
  `4f4bdd8`) still carried **~29% re-fetched-identical-fact rows** (annual
  29.3%, quarterly 28.2%) — identical on every column EXCEPT `fetched_at`,
  i.e. the same fact fetched on different days. `drop_duplicates` collapse was
  validated as exact (0 orphaned keys; the initial 8% `groupby` count was the
  unreliable method in pandas 3.x, not `drop_duplicates`).
- Ran a one-time cleanup (re-pull → sort by fetched_at → dedup all-but-
  fetched_at → push, using the production logic):
  - annual: **3,917,990 → 2,768,565** (1,149,425 re-fetch dups removed)
  - quarterly: **8,490,726 → 6,094,628** (2,396,098 removed)
  - **Total on HF now 8,863,193 rows, dup rate 0.0%.** Restatement versions
    (differ in `filed`/`value`) untouched.
- `verify_hf.py` baselines updated to the post-dedup sizes (annual ≥2.5M,
  quarterly ≥5.5M); re-ran → **VERIFY PASS**.
- **Full `validate.py` sweep: 179 PASS / 0 FAIL / 58 NO DATA.**
- Full test suite: **532 passed** (6:15).

### 6. Iceberg pilot: infra + migration of 4 tables (DONE)

User clarified scope: copy the **design** of the data-pipeline architecture (a
few pilot tables in Iceberg), not mass-migrate everything. Pilot tables:
`fundamentals_annual`, `fundamentals_quarterly`, `prices`, `macro`; everything
else stays Parquet. Approach chosen: **real `iceberg_scan` via the local
PyIceberg SQL catalog** (not globbing the parquet files under
`storage/iceberg/...`, which is what `query.py` still does for
`index_members`/`securities`/etc.).

**Diagnostic findings:**
- DuckDB 1.5.4's `iceberg_scan` reads by **metadata.json path**, not by
  catalog identifier; the path-based interface is READ-ONLY. Writing via DuckDB
  requires an attached Iceberg REST catalog (no local server — ruled out). The
  catalog-config SQL settings (`iceberg_catalog_*`) do NOT exist in this
  version.
- **The blocker and its fix:** pyiceberg's default `PyArrowFileIO` writes
  `file://C:/...` (two-slash) URIs that DuckDB's `iceberg_scan` cannot open on
  Windows. Forcing `py-io-impl=pyiceberg.io.fsspec.FsspecFileIO` with a
  **three-slash** warehouse (`file:///C:/...`) makes pyiceberg write URIs BOTH
  pyiceberg and DuckDB 1.5.4 can read — verified end-to-end (`iceberg_scan`
  returns correct counts). 3-slash warehouse + default IO breaks pyiceberg
  itself (`/C:/...` path); 2-slash warehouse breaks DuckDB reads.
- `allow_moved_paths=true` alone does NOT fix it; `mode='file'` is
  "Unimplemented" in this build.
- Existing repo tables (gscpi, constituents.*) carry the broken two-slash
  scheme, so they still scan-fail in DuckDB — out of pilot scope (left as-is).

**What was built:**
- `iceberg_pilot.py` (NEW): pilot catalog loader (`sql` type, SQLite
  `pilot_catalog.db`, fsspec IO, 3-slash warehouse), `latest_metadata()`
  (newest `*.metadata.json` for a pilot table — what `iceberg_scan` needs),
  `ensure_table()` (create-if-missing, snappy, format-version 2),
  `replace_from_parquet()` (stream full-replace of a table from a local
  parquet, one snapshot per sync).
- `migrate_pilot.py` (NEW): manual sync script — rebuilds the 4 pilot tables
  from `storage/curated/<table>/<table>.parquet` (full replace, verified via
  `iceberg_scan` after each). `--only <tables>` subset supported.
- `query.py`: `PILOT_ICEBERG_TABLES` set + `_pilot_iceberg_metadata()`;
  `_register_views` now prefers `iceberg_scan(<metadata>)` for the 4 pilot
  tables, falling back to curated parquet when the Iceberg mirror is absent
  (and raw glob as the last resort). Non-pilot tables unchanged.

**Result:** all 4 pilot tables migrated + verified via `iceberg_scan`:
macro 195,181 / fundamentals_annual 2,611,794 /
fundamentals_quarterly 3,330,614 / prices 46,953,549 rows. `query.py` views
resolve through Iceberg (`SELECT sql FROM duckdb_views()` confirms
`iceberg_scan`), filter pushdown works (AAPL 2024-01-02 price query returns
185.64). Full test suite 554 passed (3 new `TestPilotIcebergViews` tests in
`tests/test_catalog.py`); `validate.py` 179 PASS / 0 FAIL / 58 NO DATA.

**Remaining / open:**
  1. `migrate_pilot.py` is MANUAL (user's choice) — run it after `curated.py`
     whenever the Iceberg mirrors need refreshing. Not wired into `run_all.py`.
  2. Existing 2-slash Iceberg tables (`constituents.*`, `shipping.*`) are
     still DuckDB-unreadable; could be migrated to the fsspec/3-slash scheme in
     a future pass if `iceberg_scan` coverage is ever wanted for them.
  3. `prices` mirror is ~2 GB; rerunning the full sync takes ~3 minutes.

## Net result so far

Public HF dataset `ZanderL1337/financial-fundamentals` is now a DEDUPED
**8,863,193-row** dataset (annual 2,768,565 + quarterly 6,094,628; 0.0% dup
rate under the all-but-`fetched_at` key) with append-by-default wired into both
pipeline modes and a passing `verify_hf.py` post-append check. The Iceberg
pilot is DONE: 4 tables (macro/fundamentals_annual/fundamentals_quarterly/
prices, 53.1M rows) mirrored into `storage/iceberg/pilot/` via a new
`iceberg_pilot.py` + `migrate_pilot.py`, and `query.py` now reads them through
real `iceberg_scan` (blocker solved: FsspecFileIO + 3-slash warehouse). Nothing
committed locally yet.

## Next up

- `migrate_pilot.py` is manual; run it after `curated.py` to refresh the Iceberg
  mirrors (whole repo is 22 commits ahead of origin — push whenever Zander is
  ready; still parked).
- AV DJI earnings-pacing 4-option decision; Reddit/Comtrade/Census/USDA/
  AISStream blocked purely on user API keys (see `TODO.md`).

## Session 3: verify analytics/backtest against iceberg_scan — DONE

Thread chosen: confirm the 4 pilot tables served via `iceberg_scan` produce
IDENTICAL analytics + backtest results to the curated parquet path. Built
`C:\Users\zande\AppData\Local\Temp\opencode\verify_iceberg_analytics.py`
(side-by-side: view backing, row counts, schema, filtered loads, 7 analytics
outputs, backtest metrics). It surfaced 3 real bugs + 2 hardening fixes:

1. **Crash (pre-existing, both paths):** `analytics/fundamentals.py` called
   `pd.to_datetime(ann["period_end"])` with no `format`; the raw store mixes
   `YYYY-MM-DD` and `YYYY-MM-DD 00:00:00` strings → ValueError. Fixed all 3
   call sites (yoy_growth/valuation/top_by_metric) with `format="mixed"`.
2. **Nondeterminism (engine-dependent results):** `_asof_fundamentals` in
   `analytics/features.py` — one 10-K reports several fiscal years, so many
   fundamentals rows share `(symbol, filed)`; the ASOF join picked by physical
   row order, which differs between read_parquet and iceberg_scan. Backtest
   differed (-67% vs +13% total return) before the fix. Fixed by deduping the
   ASOF source to the latest fiscal year per filing:
   `ROW_NUMBER() PARTITION BY symbol, CAST(filed AS DATE)
   ORDER BY CAST(period_end AS DATE) DESC, fetched_at DESC`.
3. **Data bug (1.2M phantom duplicates):** `fundamentals_annual` raw snapshots
   store `period_end` inconsistently — `fundamentals_full_annual_20260615.parquet`
   has it as `large_string` (`'2017-09-30'`), the 20260623/20260723/20260804
   files as `timestamp[us]` (`'... 00:00:00'`). `union_by_name=True` coerces to
   strings, so the curated natural key never collapses the same fact → 1,228,766
   phantom rows (47% of 2,611,794). Fixed in `curated.py`: `_normalize_period_end()`
   canonicalizes to `%Y-%m-%d` before dedup for `fundamentals_annual`/
   `fundamentals_quarterly` (quarterly was already date-only). Rebuilt annual →
   **1,383,028 rows** (1,228,766 removed), quarterly unchanged 3,330,614.
4. **Circular compaction guard:** `query.py` now only uses `iceberg_scan` for the
   4 pilot tables when `USE_CURATED` is True (curated.py's `_raw_reads` must fall
   back to raw globs or it reads back from its own mirror).
5. **Schema drift:** `iceberg_pilot.py` gained `_schema_matches()`; 
   `replace_from_parquet()` drops + recreates the table when the mirror schema
   differs (quarterly mirror had `period_end: timestamp` vs the new string).

Migration rerun (`migrate_pilot.py --only fundamentals_annual,fundamentals_quarterly`):
annual 1,383,028 / quarterly 3,330,614, both verified via `iceberg_scan`.

**Final verification ALL CHECKS PASS** — row counts match (prices 46,953,549 /
macro 195,181 / annual 1,383,028 / quarterly 3,330,614), schemas identical,
feature_matrix (1506,30) + signal_panel (3089,7) + all 12 backtest metrics
identical (total_return -76.06%, cagr -3.38, ann_vol 12.48, sharpe -0.21,
max_drawdown -84.51). Full suite **554 passed**. Note: `.equals()` row-order
diffs between engines are expected (no ORDER BY) — verification compares
order-insensitively.

**Regression tests added (this thread):**
- `tests/test_curated.py::TestPeriodEndNormalization` (3 tests): dedup collapses
  `'2017-09-30'` vs `'2017-09-30 00:00:00'` to one fact (newest fetch wins),
  output period_end is date-only, non-fundamentals tables untouched.
- `tests/test_features.py::TestAsofFundamentalsDeterminism` (2 tests): latest
  fiscal year wins per (symbol, filed) regardless of physical row order; no
  look-ahead before `filed`. DuckDB in-memory `fundamentals_annual` monkeypatched
  via `q._con`.



**Note this is a DIFFERENT HF dataset than sections 1-6 above.** Sections 1-6
cover `ZanderL1337/financial-fundamentals` (a standalone SEC EDGAR fundamentals
dataset, pushed directly by `fundamentals_pipeline.py`'s own `hf_pull`/
`hf_append` helpers). This section covers `ZanderL1337/financial-data-pipeline`
(the full ~150-table curated snapshot, pushed by the separate
`upload_huggingface.py` script). The two datasets, two scripts, and two
append/sync mechanisms are unrelated — don't conflate them.

### What was asked

Create append functionality for the HF database used by the pipeline; make
sure new data appends correctly and duplicates are handled. Brainstorming
surfaced that `curated.py` already guarantees zero duplicate rows per table
(natural-key dedup, keep-newest-`fetched_at`) before anything reaches HF, and
that `upload_huggingface.py` was a full-folder re-upload script never wired
into any automation — clarified the actual need was **automating** that sync
(end of `run_all.py`), not building a new duplicate-detection layer.

### What was built (design spec + plan + subagent-driven-development)

- Design: `docs/superpowers/specs/2026-08-04-hf-sync-automation-design.md`
- Plan: `docs/superpowers/plans/2026-08-04-hf-sync-automation.md`
- `upload_huggingface.py`: `main()` now returns a stats dict (`repo_id`,
  `tables`, `rows`, `size_mb`, `files`) instead of `None`; also guards against
  an empty `storage/curated/` folder (returns early instead of publishing an
  empty README over the live public dataset — found in final review, see below).
- `run_all.py`: new `sync_huggingface(has_new_data, compact_enabled, dry_run,
  hf_sync_enabled) -> RunResult` — gates on 5 conditions (dry-run, new
  `--no-hf-sync` flag, `--no-compact`, no pipeline PASSed, missing
  `HF_TOKEN`/`HUGGINGFACE_TOKEN`), then uploads and verifies the upload landed
  via `HfApi().list_repo_files()`. Appears as an ordinary `hf_sync` row in the
  run summary; excluded from the overall exit-code computation so an HF-side
  hiccup can't trip the daily accumulator's failure alarm.
- Built via 3 SDD tasks in an isolated worktree, each independently reviewed
  clean (only cosmetic Minor findings deferred).

### Final whole-branch review caught real cross-task issues

The task-level reviews were each clean, but the final review (dispatched on
Opus) found: **1 Critical** — an empty local curated folder made the
post-upload verification vacuously pass and would have published a "0 tables,
0 rows" README over the live public dataset while reporting PASS (verified
reachable from the actual build worktree) — plus **3 Important** (hf_sync FAIL
was flipping the run's overall exit code → false daily-accumulator alarms; no
timeout on the HF network calls; `--no-hf-sync`'s actual effect had zero test
coverage, an inverted boolean would have passed the whole suite) and several
Minor/doc items. One fix wave addressed all of them; scoped re-review verdict:
all addressed, no new breakage. 19 new tests added across the branch (0
regressions — full suite 551/551 passing on merged master).

### Open design questions surfaced to user, NOT fixed (need a decision)

1. Post-upload verification is existence-only (`list_repo_files` diff) — it
   can't detect a genuinely-failed-but-file-still-present upload once the
   repo already has every table from a prior sync. Accepted as the approved
   design's intentional scope; flagged in case a stronger check (e.g. commit
   SHA comparison) is ever wanted.
2. Because `README.md`'s `generated_date` changes daily, any daily-scheduled
   `run_all.py --only ...` invocation (e.g. `ClaudeAuto-DailyAccumulators`)
   now triggers a guaranteed daily HF commit, even with zero underlying data
   change. `AUTOMATION.md` documents the new default-on sync behavior, but no
   `.ps1` scripts were touched — whether `daily_accumulators.ps1` should pass
   `--no-hf-sync` is an open cadence decision, not made this session.

### Result

Merged to `master` locally (merge commit `ae6b013`), worktree + branch
`worktree-hf-sync-automation` cleaned up. Not yet pushed to `origin`.
