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

## NOT yet automated (recommended next — needs Zander's go-ahead)

A daily accumulator run for the snapshot-only sources whose history only exists if
pulled daily (`tv_ratings`, `short_interest` filing dates; see EXPERT_BRIEF.md roadmap
item 1). Both are keyless/safe for unattended runs. To enable, register a daily task that
runs, from the repo root:

```
C:\ProgramData\anaconda3\python.exe run_all.py --only tradingview short_interest
```

(Verify the `--only` names against `run_all.py --dry-run` first; run_all auto-rebuilds
curated afterward. Deliberately NOT included: Schwab pipelines — OAuth is interactive
and would hang an unattended run.)

## Managing

```powershell
Get-ScheduledTask ClaudeAuto-PipelineQuality | Get-ScheduledTaskInfo   # last/next run
Start-ScheduledTask ClaudeAuto-PipelineQuality                         # run now
Unregister-ScheduledTask ClaudeAuto-PipelineQuality -Confirm:$false    # remove
```
