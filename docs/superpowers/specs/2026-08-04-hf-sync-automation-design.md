# HuggingFace Sync Automation — Design

**Date**: 2026-08-04
**Status**: Approved

## Problem

`upload_huggingface.py` re-uploads the entire `storage/curated/` folder to the
public HF dataset (`ZanderL1337/financial-data-pipeline`), but it's a manual
script — never wired into `run_all.py` or any scheduled automation. Every sync
to date has been a one-off manual run (see PROJECT_NOTES.md sync history).
Nothing verifies that a sync actually landed correctly on HF's side, and there's
no automatic check run after ingestion to confirm new data made it into the
published dataset.

Duplicate-row risk itself is already solved: `curated.dedup()` guarantees one
row per natural key (or full-row dedup as a fallback) before a table is ever
written to `storage/curated/`, and this is covered by `tests/test_curated.py`.
This design does not touch that guarantee — it only needs to make sure the
already-deduped data reliably reaches HF, and that we notice if it doesn't.

## Goals

1. Automatically sync new data to HF at the end of every `run_all.py` run —
   no manual script invocation required.
2. Never publish curated data that wasn't just recompacted (ties the automated
   sync to the existing dedup guarantee instead of building a new one).
3. Verify after every upload that the data actually landed on HF (catch
   partial/failed commits that `api.upload_folder()` could silently swallow).
4. Surface sync status in the same run summary users already read, using the
   existing `RunResult` pattern — no new UI.
5. Never let an HF outage or network failure fail the whole pipeline run.

## Non-goals

- Changing the upload model from full-folder-replace to true incremental
  row-level append. The full-replace approach is already correct (every file
  is a complete deduped snapshot) and simple; true incremental append is a
  separate, larger change not needed here.
- A new duplicate-detection or data-quality framework. `validate_table()` and
  `curated.dedup()` already cover this; this design only carries their
  guarantees forward to the HF layer.

## Design

### 1. Trigger and gating

`sync_huggingface()` is a new function in `run_all.py`, called immediately
after `compact_curated()` in `main()`. It is skipped — printing a one-line
reason, without affecting the run's exit code — when any of:

- `args.dry_run` is set
- `args.no_hf_sync` is set (new flag, mirrors `--no-compact`/`--no-validate`)
- `args.no_compact` is set (syncing un-recompacted curated data would defeat
  the ordering the whole safety property depends on)
- Neither `HF_TOKEN` nor `HUGGINGFACE_TOKEN` is set in the environment
- No pipeline in this run had `status == "PASS"` (nothing new to publish)

### 2. Upload

Reuses `upload_huggingface.main()` unchanged in spirit — full-folder upload of
`storage/curated/`. One change: `main()` currently returns `None`; it will
instead return `{"tables": int, "rows": int, "size_mb": float}` so the caller
can report a summary line. This is the only change to `upload_huggingface.py`.

### 3. Post-upload verification

After `upload_huggingface.main()` returns, call
`HfApi().list_repo_files(repo_id, repo_type="dataset")` and diff the returned
filenames against the local `storage/curated/**/*.parquet` files that were
just uploaded. Any local table missing from the remote listing means the sync
result is `FAIL` with a note listing the missing table(s). This is the only
new integrity check — it exists specifically to catch upload-layer failures
(auth hiccups, truncated/partial commits) that per-table `validate_table()`
can't see, since that only inspects local data.

### 4. Reporting

The sync step appends its own `RunResult("hf_sync", status, duration, note)`
to the `results` list before `_print_summary()` runs, so it appears as an
ordinary row in the existing summary table:

```
  PASS      hf_sync                      12.4s   150 tables, 90.2M rows, verified
```
or
```
  FAIL      hf_sync                       9.1s   2 table(s) missing remotely: x, y
```
or (skip cases)
```
  SKIP      hf_sync                        -     --no-compact set, skipping sync
```

A `FAIL` here does not change `run_all.py`'s overall exit code semantics beyond
what any other pipeline FAIL already does (existing behavior: exit 1 if any
result is not PASS/SKIP/DRY RUN — hf_sync participates in that the same way).

### 5. Error handling

The entire sync + verify sequence is wrapped in one try/except (matching
`compact_curated`'s "never let this sink a run" pattern) — any exception
(network error, HF API error) becomes a `FAIL` `RunResult` with the exception
message as the note, not an unhandled crash.

## Testing

New `tests/test_run_all.py`:

- Mocks `upload_huggingface.main` and `HfApi.list_repo_files`.
- Verifies `sync_huggingface()` is invoked exactly once when: at least one
  pipeline PASSed, compact enabled, not dry-run, `--no-hf-sync` not set, token
  present.
- Verifies it is *not* invoked (and a `SKIP` result with the right note is
  produced) for each gating condition individually: dry-run, `--no-hf-sync`,
  `--no-compact`, missing token, zero PASSes.
- Verifies a `FAIL` result is produced when the mocked remote file listing is
  missing an expected table after upload.

No new tests for dedup correctness — `tests/test_curated.py` already covers
that and this design doesn't change it.
