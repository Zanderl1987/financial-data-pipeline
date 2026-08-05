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

## Managing

```powershell
Get-ScheduledTask ClaudeAuto-PipelineQuality | Get-ScheduledTaskInfo        # last/next run
Start-ScheduledTask ClaudeAuto-PipelineQuality                              # run now
Unregister-ScheduledTask ClaudeAuto-PipelineQuality -Confirm:$false         # remove

Get-ScheduledTask ClaudeAuto-DailyAccumulators | Get-ScheduledTaskInfo      # last/next run
Start-ScheduledTask ClaudeAuto-DailyAccumulators                            # run now
Unregister-ScheduledTask ClaudeAuto-DailyAccumulators -Confirm:$false       # remove
```
