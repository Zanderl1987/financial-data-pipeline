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
  after boot if the machine was off): `fundamentals_pipeline.py --full-market --no-cache`
  (`--no-cache` forces a real EDGAR re-download + re-push instead of short-circuiting on the
  pipeline's own HF cache check) -> `curated.py` -> `verify_hf.py --repo
  ZanderL1337/financial-fundamentals`, combined output archived to `storage\quality_reports\
  fundamentals_hf_YYYY-MM-DD.txt`, one summary line per week appended to
  `storage\quality_reports\fundamentals_hf_summary_log.txt`. Added 2026-08-04 after the
  dataset was found stale for ~7 weeks (2026-06-15 -> 08-04) with no automation catching it.
  Weekly cadence chosen deliberately: SEC filings don't change fast enough to justify a daily
  ~1.3GB EDGAR re-download, and Sunday 8:00 AM avoids overlapping `ClaudeAuto-DailyAccumulators`
  (daily 9:00 AM) and `ClaudeAuto-PipelineQuality` (Monday 9:30 AM).
- **Failure signal:** `FUNDAMENTALS_HF_FAIL.txt` appears at repo root if any of the three
  steps fails (pipeline crash, `curated.py` failure, or `verify_hf.py` VERIFY FAIL — stale
  data, row-count drop, or excess duplicate rate). Any future Claude session should check for
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
