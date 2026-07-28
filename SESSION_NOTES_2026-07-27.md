# Session Notes — 2026-07-27

**Branch:** master
**Session model:** Claude Sonnet 5

## What happened

Code review of the 2026-07-26 work (QUALITY_FAIL bug-fix session + new
`logging_utils.py` framework), requested with an adversarial second pass rather than a
single review pass. Scoped to git range `892a86f..6f704ff` (the actual changed/new
code from that session) rather than the whole ~44K-line repo, since that's the
reviewable unit of new work.

Dispatched two subagents in sequence:
1. **Primary reviewer** — read the full diff plus surrounding context (not just diff
   hunks), ran `tests/test_logging.py` + `tests/test_catalog.py` itself (25 passed),
   traced each of the 4 root-cause fixes through to its schema/key/catalog consumers.
2. **Adversarial verifier** — given the primary reviewer's full report but told to
   independently re-derive every claim from source and re-run every command itself,
   not trust the first pass. Found one claim the primary reviewer had right but
   under-rated: the missing-encoding bug (see below) can silently drop captured
   output entirely on certain byte sequences, not just corrupt it — escalated from
   Important to Critical.

Both passes confirmed the 4 root-cause fixes from 07-26 and the "unwire don't delete"
pattern are genuinely correct (independently re-verified against schema/key/catalog
consumers in both passes), and that `logging_utils.py` itself is well-built with real
(non-mocked) tests.

## Issues found and fixed

1. **Critical — missing `encoding="utf-8"` in `run_all.py`'s `subprocess.run()`
   (was line 895-898).** `capture_output=True, text=True` with no explicit `encoding=`
   decodes captured stdout/stderr using `locale.getencoding()` — confirmed `cp1252` on
   this machine — even though the child is told to emit UTF-8 via `PYTHONIOENCODING`.
   Reproduced directly (not just trusted the reviewer's transcript): a child writing
   `café — dash test` came back byte-mismatched against cp1252 decode (`cafÃ©`, `â€"`
   pattern), confirmed via exact string equality checks, not just visual inspection
   (the terminal's own re-encoding made mangled and correct output look confusingly
   similar when printed — equality checks were the only reliable signal). Worse: on
   certain byte sequences (UTF-8 continuation bytes undefined in cp1252) this can raise
   inside subprocess's internal reader thread, silently truncating `result.stdout` to
   `None` — meaning a real pipeline failure gets recorded as **PASS**. Fixed: added
   `encoding="utf-8"` to the `subprocess.run()` call.
2. **Important — buffered capture broke interactive Schwab OAuth re-auth.** The 07-26
   switch to `capture_output=True` meant OAuth prompt text stayed invisible until the
   subprocess exited or timed out (default 600s), well past the documented ~30s auth
   code window. Fixed: pipelines requiring `SCHWAB_API_KEY` (8 specs) now run with
   `capture_output=False`, restoring live inherited stdio for just that family; failure
   logging falls back to a placeholder note since output isn't captured for these.
3. **Important — `CLAUDE.md:89`** still described the alphabetical-vs-mtime row-count
   bug as unfixed. Updated to reflect the 07-26 `_latest_file()` fix.
4. **Documentation — `CLAUDE.md:7`** test count was stale (454, already wrong in the
   commit that wrote it — 12 new `test_logging.py` tests weren't reflected). Corrected
   to 466, verified via `pytest --collect-only -q`.

Verified after fixes: `run_all.py` parses clean, `tests/test_logging.py` +
`tests/test_catalog.py` rerun — **25 passed**. Re-ran the encoding repro against the
patched code path to confirm the fix actually resolves the mojibake/truncation, not
just that it looks plausible.

## Left as-is (reported, not fixed — user's call)

Four Minor findings from the reviews, not applied this session:
- `run_all.py`'s generic-exception branch logs a full traceback to `run_all.log` but
  only `str(exc)` (no traceback) to the per-failure snapshot file.
- `log.*()` calls in `run_pipeline`/`_print_summary` aren't wrapped in try/except;
  `RotatingFileHandler`'s rotation isn't multi-process-safe on Windows, so an
  overlapping manual+scheduled run could in theory crash the orchestrator via the
  logging layer itself.
- `storage_utils.py:42-45`'s `find_parquet_files()` has the same alphabetical-not-mtime
  sort shape as the bug fixed in `validate.py` — currently dead code (no caller), so
  not live today, but a latent trap for a future "pick the latest file" caller.

## State / Next Up

- No open blockers from this session. The 4 Minor items above are cheap fixes if ever
  picked up, but none are live bugs today.
- Cross-repo notes from 07-26 (earnings_sentiment_tool / FDP_REPO_PATH link,
  individual-pipeline logging retrofit) are unchanged — not touched this session.
