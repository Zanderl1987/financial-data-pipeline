# TODO — 2026-08-04

## Completed This Session (see SESSION_NOTES_2026-08-04.md for detail)

- [x] **HuggingFace `financial-fundamentals` dataset refresh — DONE (2026-08-04)**: confirmed
      public via HF API; re-ran `fundamentals_pipeline.py --full-market --no-cache` (needed
      `--no-cache` to defeat the HF-cache short-circuit that otherwise returns early), 20,151
      companies / 1,329 MB ZIP processed, both `fundamentals_*_latest.parquet` files pushed to
      `ZanderL1337/financial-fundamentals` (lastModified 2026-08-04). Ran `curated.py` after
      (179 tables, 16.4M dupes removed); curated `fundamentals_annual` now 2,611,794 rows and
      `fundamentals_quarterly` 3,330,614 rows with today's `fetched_at`.
- [x] **HF dataset combined rebuild — DONE (2026-08-04)**: after the old 2026-06-15 snapshot
      briefly looked lost (was actually intact in local raw partitions + HF git history commits
      `a5741286`/`b7a71592`), rebuilt the dataset from ALL accumulated raw partitions
      (annual full_20260615 + 20260623 + 20260723 + full_20260804 -> 3,917,990 rows; quarterly
      -> 8,490,726 rows; only exact full-row dups dropped, restatement history preserved).
      Pushed to HF at commit `4f4bdd8`; datasets-server now reports **12,408,716 rows**.
      `--append` flag + `hf_append()` helper added to `fundamentals_pipeline.py` (live-tested
      as a no-op: concat + dedup -> unchanged), 142/142 pipeline tests pass.
- [x] **Append-by-default + dedup + post-append verify — DONE (2026-08-04)**: `hf_append` now
      dedups on all columns EXCEPT `fetched_at` (sort by fetched_at, NaT first, keep newest —
      unit-verified); append is the ALWAYS-default push path in both `--full-market` and DJI
      modes (`--append` flag removed; DJI mode now pushes to HF, it never did before). New
      `verify_hf.py` re-pulls both latest files from HF and checks row counts / fetched_at
      recency / symbol coverage / dup rate (fails non-zero on problems). One-time dedup cleanup
      of the live dataset pushed: annual 3,917,990 -> 2,768,565, quarterly 8,490,726 ->
      6,094,628 (29% of the combined build was re-fetched-identical-fact rows, identical in
      every column except fetched_at), **total now 8,863,193 rows, dup rate 0.0%**, restatement
      versions untouched. `verify_hf.py` VERIFY PASS; full `validate.py` sweep 179 PASS /
      0 FAIL / 58 NO DATA; full test suite 532 passed.

- [x] **Automated HF sync for the full curated dataset (`ZanderL1337/financial-data-pipeline`) — MERGED (2026-08-04)**:
      NOT the same dataset/mechanism as the two items above (those are
      `ZanderL1337/financial-fundamentals` via `fundamentals_pipeline.py`'s own
      `hf_pull`/`hf_append`). `upload_huggingface.py` was a manual, never-automated
      full-folder re-upload script; wired it into `run_all.py` as a final stage
      (new `sync_huggingface()`, `--no-hf-sync` flag) that syncs automatically after
      every run, gated on compact having run + new data + a token being present,
      and verifies the upload landed via `HfApi().list_repo_files()`. Built via
      subagent-driven-development in an isolated worktree (spec + plan committed
      under `docs/superpowers/{specs,plans}/2026-08-04-hf-sync-automation*`); 3
      tasks each reviewed clean, but the final whole-branch review (Opus) caught 1
      Critical (empty curated folder -> vacuous PASS + would've published an empty
      README over the live public dataset) + 3 Important (hf_sync FAIL was flipping
      run_all.py's exit code -> false daily-accumulator alarms; no network timeout;
      `--no-hf-sync`'s actual effect had zero test coverage) — one fix wave
      addressed all of them, re-review confirmed clean. 19 new tests, 0
      regressions (551/551 passing). Merged to `master` (`ae6b013`), worktree/branch
      cleaned up. Two open decisions surfaced to user, RESOLVED (2026-08-04, see
      AUTOMATION.md "Two design questions resolved"): (1) verification depth stays
      existence-check-only (HF's own upload commits are already hash-verified
      server-side); (2) `daily_accumulators.ps1` does NOT need `--no-hf-sync` — its
      3 pipelines produce genuinely new rows every day by design, so daily HF sync
      is intended behavior, not churn.

- [x] **HF `financial-fundamentals` refresh automation — DONE (2026-08-04)**: new
      `scripts/fundamentals_hf_refresh.ps1` (mirrors `daily_accumulators.ps1`/
      `weekly_data_quality.ps1` pattern) runs `fundamentals_pipeline.py --full-market
      --no-cache` -> `curated.py` -> `verify_hf.py`, archiving combined output to
      `storage\quality_reports\fundamentals_hf_YYYY-MM-DD.txt` with
      `FUNDAMENTALS_HF_FAIL.txt` flag on any step failing. Registered as Windows
      Scheduled Task `ClaudeAuto-FundamentalsHFRefresh`, weekly Sunday 8:00 AM
      (user chose weekly over daily/monthly — SEC filings don't change fast enough
      to justify daily ~1.3GB re-downloads; offset from the other two automations'
      schedules). See AUTOMATION.md for full detail.

## Completed Prior Sessions (see SESSION_NOTES_2026-08-01.md / SESSION_NOTES_2026-08-03.md for detail)

- [x] **FRED 9 broken series IDs** — already fixed in a prior session, undocumented; verified live.
- [x] **OECD MEI pipeline rewrite** — already done 2026-07-29, undocumented; verified live.
- [x] **pandas 3.0.3 `read_html` regression** — fixed in `finviz_pipeline.py` + `stockanalysis_pipeline.py`.
- [x] **`dark_pool_pipeline.py` rewrite** — source endpoint retired; rewired to `api.finra.org`.
- [x] **`eia_expansion_pipeline.py`** — 5 distinct API param bugs fixed, all 7 sub-datasets live.
- [x] **`open_meteo_pipeline.py`** — not a bug; incremental mode works, backfill needs chunking (see Backlog).
- [x] **Interactive backtest explorer (`backtest_app.py`) — MERGED (2026-08-03)**: new live
      Dash app on top of the existing `evaluation/` framework (see
      `SESSION_NOTES_2026-08-03.md`, spec at `docs/superpowers/specs/
      2026-08-03-interactive-backtest-explorer-design.md`, plan at `docs/superpowers/
      plans/2026-08-03-interactive-backtest-explorer.md`). Built via subagent-driven
      development in an isolated worktree; 10/10 plan tasks complete, task review caught
      2 real defects (a dash/comm import bug, a silent boundary-operator deviation), final
      whole-branch review caught 7 more (cache staleness on Refresh, a permanently-blank
      IC panel for the only tunable signal, symbol-switch triggering a full recompute,
      missing loading spinners, a None-format crash, 2 stray committed files) — all fixed
      and re-reviewed clean. Fast-forward merged to `master` (`69912da`), worktree/branch
      cleaned up. 532/532 tests passing. Not yet pushed to `origin`.

## In Progress

- [x] **Iceberg pilot migration (fundamentals_annual, fundamentals_quarterly, prices, macro) — DONE (2026-08-04)**:
      all 4 pilot tables migrated + verified via real DuckDB `iceberg_scan` (macro 195,181 /
      fundamentals_annual 2,611,794 / fundamentals_quarterly 3,330,614 / prices 46,953,549 rows).
      Blocker solved: pyiceberg's default PyArrowFileIO writes two-slash `file://C:/...` URIs that
      DuckDB can't open on Windows; forcing `py-io-impl=pyiceberg.io.fsspec.FsspecFileIO` with a
      THREE-slash warehouse (`file:///C:/...`) makes pyiceberg write URIs BOTH tools read (verified
      end-to-end). New `iceberg_pilot.py` (pilot catalog loader + `latest_metadata()` +
      `replace_from_parquet()` full-replace sync) and `migrate_pilot.py` (manual sync script,
      `--only` subset supported). `query.py` `_register_views` now prefers `iceberg_scan(<metadata>)`
      for the 4 pilot tables with curated-parquet fallback; non-pilot tables unchanged. 3 new tests
      in `tests/test_catalog.py` (554 total passing); `validate.py` 179 PASS / 0 FAIL / 58 NO DATA.
      NOTE: `migrate_pilot.py` is MANUAL (user's choice) — rerun after `curated.py` to refresh.
- [x] **AV earnings backfill pacing — decision made, coordination fixed (2026-08-04)**:
      user chose "coordinate a schedule split" over paying for AV premium, dropping AV,
      or leaving it manual. Root cause of the 07-31/08-01 10:05am retry failures found:
      AV's quota is rolling-24h not midnight-reset, so a 10:05am run (before
      earnings_sentiment_tool's 10:30am TranscriptPull) still saw yesterday's ~10:30
      usage as unexpired. Fixed by promoting `scripts/av_earnings_pacing.ps1` from a
      one-time task to a real recurring Windows Scheduled Task
      `ClaudeAuto-AVEarningsPacing`, 10:45am (after TranscriptPull, not before). Also
      fixed a stale cross-repo reference in PROJECT_NOTES.md that misnamed the
      AV-quota-sharing repo `custom_index_tool` (unrelated, FRED-only) instead of
      `earnings_sentiment_tool`. Still 9/30 DJI symbols and will keep reporting QUOTA
      most days until earnings_sentiment_tool's transcript cache completes (~08-06,
      per that repo's own ETA) and its daily pull becomes a zero-quota no-op — no
      further action needed, this is expected and self-resolving. See AUTOMATION.md.
- [ ] **Patents pipeline rewrite** — blocked: need ODP API key from USPTO.gov account.
- [ ] **Full Schwab price-history backfill** — sizing + running as of 2026-08-01 session end.

## Backlog

- [x] **`finra_short_interest` rewrite — RULED OUT (2026-08-02)**: the keyless
      `api.finra.org/data/group/otcMarket/name/equityShortInterest` endpoint flagged
      2026-08-01 turned out to be OTC/pink-sheet-scoped, not NMS — verified live,
      AAPL/MSFT/TSLA/SPY all return 204 (no rows). The real NMS-consolidated dataset
      (what `short_pressure` actually needs, matching the old dead CNMSshvol CDN file)
      lives at `equityMarket/equityShortInterest`, confirmed 401 keyless — still needs
      registered FINRA Query API credentials from developer.finra.org, unchanged from
      the 2026-07-06 finding. No further action without those credentials; yfinance
      fallback in `analytics/features.py` remains the correct active source.
- [x] **`open_meteo_pipeline.py --backfill` date-chunking — DONE (2026-08-03)**: added
      `_date_chunks()` (3yr windows, 13 chunks for 1990-2026) so `main()` iterates
      date-chunk x location-batch instead of one 35yr call. Verified: chunk boundaries
      are contiguous/gap-free via standalone test, live-tested `_fetch_batch` against
      the real API (10-day window, Des Moines, 10 rows returned correctly) before
      running full backfill in background.
- [x] **BLS retry — RULED OUT as a "just retry" fix (2026-08-03)**: re-ran
      `bls_oes_qcew_pipeline.py` fresh — still hit `"the daily threshold ... has been
      reached"` immediately. Root cause isn't a stale quota, it's structural: no
      `BLS_API_KEY` is set in `.env`, so the pipeline runs BLS's **keyless v1 API**,
      which has a very low shared anonymous-IP daily quota that a single run's own
      ~1000+ series/chunk requests exhausts by itself. A free v2 key (instant
      self-service signup, no approval wait, at https://data.bls.gov/registrationEngine/)
      raises the limit to 500 req/day and batch size from 25→50 series/call
      (`bls_oes_qcew_pipeline.py` already has the v2 code path — just reads
      `BLS_API_KEY` from `.env` if present). No further retry will help without it.
- [ ] **Reddit/Comtrade/Census/USDA/AISStream** — all NO-DATA, all blocked purely on the user
      obtaining/renewing an API key or app registration (no code issue).
