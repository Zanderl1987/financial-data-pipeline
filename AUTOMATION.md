# Automation — financial-data-pipeline

Set up 2026-07-06 (by Claude, with Zander's approval).

## ClaudeAuto-PipelineQuality (Windows Scheduled Task)

- **What:** runs `scripts\weekly_data_quality.ps1` every Monday 9:30 AM (catches up after
  boot if the machine was off): `validate.py` over all tables, full output archived to
  `storage\quality_reports\validate_YYYY-MM-DD.txt`, one summary line per week appended
  to `storage\quality_reports\summary_log.txt`.
- **Failure signal:** `QUALITY_FAIL.txt` appears at repo root when validate.py reports
  any FAIL. Any future Claude session should check for that file. Auto-clears on the
  next clean run.

## ClaudeAuto-DailyAccumulators (Windows Scheduled Task)

- **What:** runs `scripts\daily_accumulators.ps1` every day at 9:00 AM (catches up
  after boot if the machine was off): `run_all.py --only tradingview,short_interest,
  finnhub_events`, output archived to `storage\quality_reports\accumulators_YYYY-MM-DD.txt`,
  one summary line per day appended to `storage\quality_reports\accumulators_summary_log.txt`.
  All three are keyless/unattended-safe: history only exists if pulled daily (`tv_ratings`,
  `short_interest` filing dates, `earnings_calendar`) — missed days are permanent gaps,
  no backfill recovers them (confirmed 2026-07-23: Finnhub's free tier will not return
  earnings_calendar rows older than ~1 year even with `--backfill`, so incremental daily
  accumulation is the only way this table gets deeper). Deliberately NOT included: Schwab
  pipelines (`schwab_movers` etc.) — OAuth is interactive and would hang an unattended run.
- **Failure signal:** `DAILY_ACCUMULATOR_FAIL.txt` appears at repo root when run_all.py
  reports any FAIL. Any future Claude session should check for that file. Auto-clears on
  the next clean run.

## ClaudeAuto-DailyStage1 (Windows Scheduled Task)

- **What:** runs `scripts\daily_stage1.ps1` every day at 3:00 AM (catches up after boot if
  the machine was off): `run_all.py --stage 1 --skip <15 names>`, output archived to
  `storage\quality_reports\stage1_YYYY-MM-DD.txt`, one summary line per day appended to
  `storage\quality_reports\stage1_summary_log.txt`. 62 pipelines selected, 3 skip for
  missing env, so ~59 actually run. Budget 2-2.5h (a full 86-pipeline run measures ~173 min);
  `ExecutionTimeLimit` is 6h.
- **Why it exists:** added 2026-08-11. Until then the only scheduled *fetching* was
  `ClaudeAuto-DailyAccumulators` — seven pipelines. Everything else refreshed only when a
  human happened to run `run_all.py`, and a `fetched_at` sweep found 11 of 182 curated tables
  stale in clusters dated to those manual runs (`cot` 56d, `alpha_vantage_forex`/`_technical`
  49d, `bls_*` 27d, `eia_*`/`fred_rates_gdp`/`treasury_exchange_rates` 19d, `prices` 9d).
  None of those pipelines were broken. Nothing ran them.
- **Why `--stage 1 --skip` and not `--only <list>`:** a hand-maintained `--only` list is the
  same defect one level up — add a stage-1 pipeline, forget the list, and it is silently never
  scheduled, which is exactly how six sources went dark for six weeks before `891d97d`.
  `--stage 1` picks up new pipelines automatically; only the skip list is hand-maintained, and
  it names things that are known bad, which changes slowly. `tests/test_catalog.py::
  TestScheduledJobSkipLists` fails the suite if a skip entry stops matching a real pipeline
  (`--skip` ignores unknown names silently, so a typo would otherwise be invisible).
- **Excluded, and why:** metered keys — `alpha_vantage_fundamentals` (25/day, reserved for
  `ClaudeAuto-AVEarningsPacing`), `bls_expansion`/`bls_oes_qcew` (BLS keyless 25/day),
  `eia`/`eia_expansion`/`eia_petng_prices`/`eia_hourly_grid`, `gas_prices`. Known-dead per
  CLAUDE.md — `nasdaq_data_link`, `usda`, `trade`, `congressional_trades` (they fail every run
  and would keep the job permanently red, masking real failures). Already daily —
  `tradingview`, `short_interest`, `finnhub_events`.
- **Does NOT cover:** stage 2 (Schwab — interactive OAuth) or stage 3 (derived: `alpha_vantage`,
  `signal_monitor`). `prices`, `alpha_vantage_forex`, `alpha_vantage_technical` and
  `signal_health` therefore still need another route.
- **Failure signal:** `STAGE1_FAIL.txt` at repo root, naming the failed pipelines and the
  archived report. Auto-clears on the next clean run.
- **Note — this makes two full HuggingFace syncs per day.** `run_all.py` syncs the whole
  curated snapshot at the end of every run, so 3:00 AM and 9:00 AM now each upload it. Nothing
  is lost either way, but if the bandwidth matters, the one to disable is the 9:00 accumulator
  job (`--no-hf-sync`) — the 3:00 run carries far more new data.

## HuggingFace dataset sync (`run_all.py`)

`run_all.py` automatically syncs `storage/curated/` to the public HuggingFace dataset
(`ZanderL1337/financial-data-pipeline`) at the end of every run, via `upload_huggingface.py`.
The sync is gated on all of: `HF_TOKEN`/`HUGGINGFACE_TOKEN` being set, `--no-compact` not
being passed (curated compaction must have had a chance to run this session), and at least
one pipeline having PASSed. It uploads the full curated snapshot, then verifies the upload
by listing remote files — not just the tables that ran this session. This can be disabled
per-invocation with `--no-hf-sync`. An `hf_sync` FAIL (e.g. rate limit, transient network
error, expired token) is reported in the summary table but does **not** flip `run_all.py`'s
overall exit code — it's an HF-side concern, distinct from a pipeline/data-collection
failure, so it will not trigger `DAILY_ACCUMULATOR_FAIL.txt` on its own.

**Two design questions resolved 2026-08-04:**
- *Verification depth:* existence-check (`HfApi().list_repo_files()`) only, no
  content/SHA comparison. Kept as-is — HF's `upload_folder` commits are hash-verified
  on the HF side already (git-lfs/Xet content addressing), so "file exists remotely
  but is silently corrupted" is not a realistic failure mode this needs to guard
  against. Revisit only if a real corruption incident is ever observed.
- *`daily_accumulators.ps1` and `--no-hf-sync`:* NOT added. The 3 accumulator
  pipelines (`tradingview`, `short_interest`, `finnhub_events`) are specifically
  the ones where "history only exists if pulled daily" — each run produces
  genuinely new rows (new filing dates, new calendar entries), not just a
  same-data re-upload. Syncing those to HF daily is the intended behavior, not
  spurious churn from the README's date stamp.

## ClaudeAuto-AVEarningsPacing (Windows Scheduled Task)

- **What:** runs `scripts\av_earnings_pacing.ps1` every day at 10:45 AM:
  `alpha_vantage_fundamentals_pipeline.py` (incremental, default 20-request budget) to
  pace the DJI-30 earnings-history backfill (`alpha_vantage_earnings`/
  `alpha_vantage_earnings_calendar` CATALOG tables), still at 9/30 symbols as of
  2026-08-04. Output archived to `storage\quality_reports\av_earnings_pacing_YYYY-MM-DD.txt`.
- **Cross-repo quota coordination (decided 2026-08-04):** Alpha Vantage's 25 req/day
  quota is shared **per IP, rolling-24h** (not per-key, not midnight-reset) across this
  machine's two repos — this one and `earnings_sentiment_tool`, whose own
  `ClaudeAuto-TranscriptPull` fires daily at 10:30 AM. A one-time retry at 10:05 AM
  (BEFORE 10:30) was tried on 07-31 and 08-01 and failed both times with zero progress,
  because rolling-24h means yesterday's ~10:30 usage doesn't roll off until ~10:30
  *today* — a 10:05 run still saw it as unexpired quota. Fixed by scheduling this task
  AFTER TranscriptPull's run (10:45, not 10:05) instead of before it. This will still
  legitimately report `QUOTA` status most days until `earnings_sentiment_tool`'s
  725-file transcript cache finishes (~2026-08-06 per that repo's own ETA) and its
  daily pull becomes a zero-quota no-op — at that point this task starts making real
  daily progress automatically, no further changes needed.
- **Failure signal:** `AV_EARNINGS_PACING_FAIL.txt` appears at repo root — `FAIL` status
  means the pipeline crashed, `QUOTA` status means it hit AV's rate limit with no real
  progress (expected during the coordination window above, not itself an error). Any
  future Claude session should check for that file. Auto-clears on the next OK run.

## ClaudeAuto-FundamentalsHFRefresh (Windows Scheduled Task)

- **What:** runs `scripts\fundamentals_hf_refresh.ps1` every Sunday at 8:00 AM (catches up
  after boot if the machine was off): `fundamentals_pipeline.py --full-market` (extraction
  only; the pipeline no longer pushes raw files to HF) -> `curated.py` ->
  `build_fundamentals_dataset.py` (assembles the Option-D snapshot — facts/companies/
  filings/wide-latest/metrics — and pushes all files to HF in ONE atomic commit, one
  coherent revision per run) -> `verify_hf.py --repo
  ZanderL1337/financial-fundamentals`, combined output archived to `storage\quality_reports\
  fundamentals_hf_YYYY-MM-DD.txt`, one summary line per week appended to
  `storage\quality_reports\fundamentals_hf_summary_log.txt`. Added 2026-08-04 after the
  dataset was found stale for ~7 weeks (2026-06-15 -> 08-04) with no automation catching it.
  Weekly cadence chosen deliberately: SEC filings don't change fast enough to justify a daily
  ~1.3GB EDGAR re-download, and Sunday 8:00 AM avoids overlapping `ClaudeAuto-DailyAccumulators`
  (daily 9:00 AM) and `ClaudeAuto-PipelineQuality` (Monday 9:30 AM).
- **2026-08-05 schema change:** dataset converted from two long files to the 5-file
  Option-D snapshot (foreign issuers via `ifrs-full`, forms 20-F/40-F/6-K/10-K/A/10-Q/A/8-K,
  accession-tracked restatements). Old `financials_*_latest.parquet` filenames now hold the
  WIDE latest-filing-wins tables. See `SESSION_NOTES_2026-08-05.md`.
- **Failure signal:** `FUNDAMENTALS_HF_FAIL.txt` appears at repo root if any of the four
  steps fails (pipeline crash, `curated.py` failure, `build_fundamentals_dataset.py` failure,
  or `verify_hf.py` VERIFY FAIL — stale data, row-count drop, excess duplicate rate, or
  snapshot.json/actual row-count mismatch). Any future Claude session should check for
  that file. Auto-clears on the next clean run.

## Managing

```powershell
Get-ScheduledTask ClaudeAuto-PipelineQuality | Get-ScheduledTaskInfo        # last/next run
Start-ScheduledTask ClaudeAuto-PipelineQuality                              # run now
Unregister-ScheduledTask ClaudeAuto-PipelineQuality -Confirm:$false         # remove

Get-ScheduledTask ClaudeAuto-DailyAccumulators | Get-ScheduledTaskInfo      # last/next run
Start-ScheduledTask ClaudeAuto-DailyAccumulators                            # run now
Unregister-ScheduledTask ClaudeAuto-DailyAccumulators -Confirm:$false       # remove

Get-ScheduledTask ClaudeAuto-FundamentalsHFRefresh | Get-ScheduledTaskInfo  # last/next run
Start-ScheduledTask ClaudeAuto-FundamentalsHFRefresh                       # run now
Unregister-ScheduledTask ClaudeAuto-FundamentalsHFRefresh -Confirm:$false  # remove

Get-ScheduledTask ClaudeAuto-AVEarningsPacing | Get-ScheduledTaskInfo      # last/next run
Start-ScheduledTask ClaudeAuto-AVEarningsPacing                            # run now
Unregister-ScheduledTask ClaudeAuto-AVEarningsPacing -Confirm:$false       # remove
```
